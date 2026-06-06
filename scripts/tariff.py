#!/usr/bin/env python3
"""
tariff.py  —  Arrol LiDAR analysis stage: stand tariff number + volume
======================================================================
Computes an average (stand/crop) TARIFF NUMBER using the UK Forestry
Commission tariff system (the "Blue Book" — FC Booklet 36 / Forest
Mensuration Handbook Booklet 39) and the total MERCHANTABLE volume
(to a 7 cm top diameter) of the stand.

It consumes artifacts already produced by the pipeline:

  --results   results.csv         per-tree DBH (the ~50% with a fitted stem)
                                  cols: tree_id, dbh_cm, tree_height_m,
                                        status, dbh_confidence, ...
  --treetops  treetops.csv        the full population + height (~95%)
                                  cols: tree_id, tree_top_x, tree_top_y,
                                        canopy_height_m
  --profile   stem_profile.csv    radius-at-height per stem (best volume)
                                  cols: tree_id, z_m, radius_m, diameter_cm,
                                        diameter_source, profile_confidence

Method
------
  1.  VOLUME SAMPLE TREES — trees whose volume we can measure directly by
      integrating their stem profile (Smalian) up to the 7 cm top. These
      are the FC "volume sample trees" (non-destructive LiDAR equivalent
      of felled sample trees). Trees with a DBH but no usable profile fall
      back to a form-factor volume from DBH + height.
  2.  Each sample tree's TARIFF NUMBER is derived by inverting the FC
      volume / basal-area line.
  3.  STAND TARIFF NUMBER = (robust) mean of the sample-tree tariffs.
  4.  Trees with a height but no DBH get a DBH imputed from a DBH~height
      regression fitted on trees that have both (handles the 50/95 gap).
  5.  The stand tariff is applied to EVERY tree's basal area to give a
      per-tree merchantable volume; these are summed for the stand total.

Outputs
-------
  --out-summary  tariff_summary.json   flat headline numbers (stat cards)
  --out-csv      tariff.csv            per-tree table (download + analytics)

Pure standard library (csv, json, math, statistics, argparse) — no numpy
or pandas, so the stage cannot fail on a missing import.

NOTE: a field-survey estimate, not a formal certified mensuration result.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from typing import Dict, List, Optional, Sequence, Any, Tuple


# ---------------------------------------------------------------------------
# Forestry Commission tariff line  (verified)
#     v  = a1 + a2 * ba
#     a2 = K * (T - C)
#     a1 = E * T - a2 * D
# ---------------------------------------------------------------------------
_E = 0.0360541
_K = 0.315049301
_C = 0.138763302
_D = 0.118288
MERCH_TOP_DIAM_M = 0.07            # 7 cm top — merchantability limit
BUTT_SWELL = 1.12                  # ground-section diameter vs lowest slice
FALLBACK_FORM_FACTOR = 0.42        # conifer-typical; only for DBH-no-profile trees

DEFAULT_FORM_FACTORS: Dict[str, float] = {
    "SS": 0.42, "NS": 0.42, "SP": 0.43, "DF": 0.44,
    "LP": 0.41, "EL": 0.40, "JL": 0.40, "HL": 0.40,
}


def basal_area_m2(dbh_cm: float) -> float:
    return math.pi * (dbh_cm ** 2) / 40_000.0


def volume_from_tariff(tariff: float, ba_m2: float) -> float:
    a2 = _K * (tariff - _C)
    a1 = _E * tariff - a2 * _D
    return a1 + a2 * ba_m2


def tariff_from_volume(volume_m3: float, ba_m2: float) -> float:
    """T = [v + K*C*(ba - D)] / [E + K*(ba - D)]  (inverse of the line)."""
    g = ba_m2 - _D
    denom = _E + _K * g
    if abs(denom) < 1e-12:
        return float("nan")
    return (volume_m3 + _K * _C * g) / denom


def form_factor_volume(dbh_cm: float, height_m: float, ff: float) -> float:
    r = (dbh_cm / 100.0) / 2.0
    return math.pi * r * r * height_m * ff


# ---------------------------------------------------------------------------
# Smalian merchantable volume from a radius-at-height profile
# ---------------------------------------------------------------------------
def merch_volume_from_profile(zr: List[Tuple[float, float]]) -> Optional[float]:
    """Merchantable stem volume (m3, to 7 cm top) from (z_m, radius_m) pairs.

    Adds a butt section from ground if the profile starts above it, and
    truncates at the height where diameter falls to 7 cm (interpolated).
    Returns None if there is not enough profile to integrate.
    """
    pts = sorted((z, r) for z, r in zr if r is not None and r > 0 and z is not None)
    if len(pts) < 2:
        return None

    # Butt section: extend down to ground using a modest swell factor.
    if pts[0][0] > 0.3:
        pts.insert(0, (0.0, pts[0][1] * (BUTT_SWELL / 2.0 + 0.5)))

    top_r = MERCH_TOP_DIAM_M / 2.0
    vol = 0.0
    for (z0, r0), (z1, r1) in zip(pts, pts[1:]):
        # Stop at the 7 cm top: interpolate the partial section then finish.
        if r1 <= top_r <= r0 and r0 != r1:
            frac = (r0 - top_r) / (r0 - r1)
            z_top = z0 + frac * (z1 - z0)
            a0 = math.pi * r0 * r0
            a_top = math.pi * top_r * top_r
            vol += (a0 + a_top) / 2.0 * (z_top - z0)
            return vol
        a0 = math.pi * r0 * r0
        a1 = math.pi * r1 * r1
        vol += (a0 + a1) / 2.0 * (z1 - z0)
    # Profile ended before reaching the 7 cm top — return what we have.
    return vol


# ---------------------------------------------------------------------------
# Small CSV helpers (tolerant of missing / blank / NaN cells)
# ---------------------------------------------------------------------------
def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in ("na", "nan", "none", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _read_csv(path: Optional[str]) -> List[Dict[str, str]]:
    if not path:
        return []
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))
    except FileNotFoundError:
        return []


def _pick(row: Dict[str, str], *names: str) -> Optional[str]:
    for n in names:
        if n in row and str(row[n]).strip() != "":
            return row[n]
    return None


# ---------------------------------------------------------------------------
# DBH ~ height OLS (for imputing the missing DBHs)
# ---------------------------------------------------------------------------
def _fit_dbh_height(pairs: Sequence[Tuple[float, float]]) -> Dict[str, Any]:
    ys = [p[1] for p in pairs]
    mean_dbh = statistics.fmean(ys) if ys else 0.0
    if len(pairs) < 3:
        return {"type": "mean", "mean_dbh": mean_dbh, "n": len(pairs),
                "slope": None, "intercept": None, "r2": None}
    xs = [p[0] for p in pairs]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return {"type": "mean", "mean_dbh": mean_dbh, "n": len(pairs),
                "slope": None, "intercept": None, "r2": None}
    b = sxy / sxx
    a = my - b * mx
    syy = sum((y - my) ** 2 for y in ys)
    r2 = (sxy ** 2) / (sxx * syy) if syy > 0 else 0.0
    return {"type": "linear", "slope": b, "intercept": a, "r2": r2,
            "n": len(pairs), "mean_dbh": mean_dbh}


def _predict_dbh(model: Dict[str, Any], height_m: Optional[float]) -> Optional[float]:
    if model["type"] == "linear" and height_m is not None:
        d = model["intercept"] + model["slope"] * height_m
        if d > 0:
            return d
    return model.get("mean_dbh")


# ---------------------------------------------------------------------------
# Compartment assignment (point-in-polygon) + robust tariff
# ---------------------------------------------------------------------------
def _robust_mean_tariff(tariffs: Sequence[float], no_reject: bool, outlier_sd: float) -> Tuple[float, int]:
    """Mean tariff after rejecting outliers beyond `outlier_sd` SDs. Returns (mean, n_dropped)."""
    kept = list(tariffs)
    dropped = 0
    if not no_reject and len(tariffs) >= 4:
        m = statistics.fmean(tariffs)
        sd = statistics.pstdev(tariffs)
        if sd > 0:
            k = [x for x in tariffs if abs(x - m) <= outlier_sd * sd]
            if k:
                dropped = len(tariffs) - len(k)
                kept = k
    return statistics.fmean(kept), dropped


def _ring_contains(x: float, y: float, ring: Sequence[Sequence[float]]) -> bool:
    """Ray-casting point-in-ring (ring = list of [lon, lat])."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _geom_contains(x: float, y: float, geom: Dict[str, Any]) -> bool:
    """Point-in-Polygon / MultiPolygon, honouring holes."""
    t = geom.get("type")
    if t == "Polygon":
        rings = geom.get("coordinates") or []
        if not rings or not _ring_contains(x, y, rings[0]):
            return False
        return not any(_ring_contains(x, y, h) for h in rings[1:])
    if t == "MultiPolygon":
        for poly in geom.get("coordinates") or []:
            if poly and _ring_contains(x, y, poly[0]) and not any(_ring_contains(x, y, h) for h in poly[1:]):
                return True
        return False
    return False


def _geom_bbox(geom: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    xs: List[float] = []
    ys: List[float] = []

    def walk(co: Any) -> None:
        if isinstance(co, (list, tuple)):
            if co and isinstance(co[0], (int, float)):
                xs.append(co[0]); ys.append(co[1])
            else:
                for c in co:
                    walk(c)

    walk(geom.get("coordinates"))
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _load_compartments(path: Optional[str]) -> List[Dict[str, Any]]:
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            gj = json.load(fh)
    except Exception:
        return []
    comps: List[Dict[str, Any]] = []
    for f in gj.get("features", []):
        geom = f.get("geometry")
        if not geom:
            continue
        p = f.get("properties") or {}
        comps.append({
            "id": p.get("id"),
            "ref": p.get("ref") or (str(p.get("id")) if p.get("id") is not None else "?"),
            "area_hectares": _f(p.get("area_hectares")),
            "geom": geom,
            "bbox": _geom_bbox(geom),
        })
    return comps


def _assign_compartment(lon: float, lat: float, comps: List[Dict[str, Any]]) -> Optional[Any]:
    for c in comps:
        bb = c["bbox"]
        if bb and (lon < bb[0] or lon > bb[2] or lat < bb[1] or lat > bb[3]):
            continue
        if _geom_contains(lon, lat, c["geom"]):
            return c["id"]
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Stand tariff number + merchantable volume (FC tariff system).")
    ap.add_argument("--results",  required=True, help="Per-tree DBH results CSV.")
    ap.add_argument("--treetops", required=True, help="Tree-tops CSV (full population + height).")
    ap.add_argument("--profile",  required=False, default=None, help="Stem-profile CSV (radius at height).")
    ap.add_argument("--out-summary", required=True, help="Output headline summary JSON.")
    ap.add_argument("--out-csv",     required=True, help="Output per-tree tariff/volume CSV.")
    ap.add_argument("--out-geojson", default=None,
                    help="Optional per-tree merch-volume GeoJSON (points, WGS84) for the map / compartment rollup.")
    ap.add_argument("--crs", default="EPSG:27700", help="CRS of the input x/y (reprojected to EPSG:4326 for the GeoJSON).")
    ap.add_argument("--compartments", default=None,
                    help="Optional compartments GeoJSON (WGS84) — refit a tariff number per compartment.")
    ap.add_argument("--out-compartments", default=None,
                    help="Optional per-compartment tariff/volume CSV.")
    ap.add_argument("--min-compartment-samples", type=int, default=4,
                    help="Min volume-sample trees in a compartment to refit its own tariff (else inherit stand).")
    ap.add_argument("--species", default=None, help="Species code for fallback form factor (e.g. SS, DF).")
    ap.add_argument("--form-factor", type=float, default=None, help="Override fallback form factor.")
    ap.add_argument("--min-confidence", type=float, default=0.40,
                    help="Min dbh_confidence for a tree to be a volume sample tree.")
    ap.add_argument("--no-reject", action="store_true", help="Do not reject tariff outliers.")
    ap.add_argument("--outlier-sd", type=float, default=2.5, help="Outlier rejection threshold (SDs).")
    args = ap.parse_args(argv)

    warnings: List[str] = []

    # ---- form factor for the DBH-no-profile fallback path ----
    if args.form_factor is not None:
        fallback_ff = args.form_factor
    elif args.species and args.species in DEFAULT_FORM_FACTORS:
        fallback_ff = DEFAULT_FORM_FACTORS[args.species]
    else:
        fallback_ff = FALLBACK_FORM_FACTOR

    # ---- load DBH results (the ~50%) keyed by tree_id ----
    dbh: Dict[int, Dict[str, Any]] = {}
    for row in _read_csv(args.results):
        tid_s = _pick(row, "tree_id", "id")
        if tid_s is None:
            continue
        try:
            tid = int(float(tid_s))
        except ValueError:
            continue
        dbh[tid] = {
            "dbh_cm": _f(_pick(row, "dbh_cm")),
            "height_m": _f(_pick(row, "tree_height_m", "height_m", "canopy_height_m")),
            "status": (_pick(row, "status") or "").strip(),
            "confidence": _f(_pick(row, "dbh_confidence")),
        }

    # ---- load the full population + heights (the ~95%) ----
    population: Dict[int, Dict[str, Any]] = {}
    for row in _read_csv(args.treetops):
        tid_s = _pick(row, "tree_id", "id")
        if tid_s is None:
            continue
        try:
            tid = int(float(tid_s))
        except ValueError:
            continue
        population[tid] = {
            "height_m": _f(_pick(row, "canopy_height_m", "height_m")),
            "x": _f(_pick(row, "tree_top_x", "x")),
            "y": _f(_pick(row, "tree_top_y", "y")),
        }
    # Make sure every DBH tree is in the population even if absent from treetops.
    for tid, d in dbh.items():
        population.setdefault(tid, {"height_m": d.get("height_m"), "x": None, "y": None})
        if population[tid].get("height_m") is None:
            population[tid]["height_m"] = d.get("height_m")

    if not population:
        raise SystemExit("tariff.py: no trees found in treetops or results CSV.")

    # ---- profile -> per-tree (z, r) lists for Smalian volume ----
    prof: Dict[int, List[Tuple[float, float]]] = {}
    for row in _read_csv(args.profile):
        tid_s = _pick(row, "tree_id", "id")
        z = _f(_pick(row, "z_m"))
        r = _f(_pick(row, "radius_m"))
        if r is None:
            d_cm = _f(_pick(row, "diameter_cm"))
            r = (d_cm / 200.0) if d_cm else None
        if tid_s is None or z is None or r is None:
            continue
        try:
            tid = int(float(tid_s))
        except ValueError:
            continue
        prof.setdefault(tid, []).append((z, r))

    # ---- Steps 1+2: derive tariff numbers from volume sample trees ----
    sample_tariffs: List[float] = []
    sample_t_by_tid: Dict[int, float] = {}  # tree_id -> its own tariff (for per-compartment refit)
    sample_kind: Dict[int, str] = {}        # tree_id -> "profile" | "form_factor"
    sample_vol: Dict[int, float] = {}
    for tid, d in dbh.items():
        dbh_cm = d.get("dbh_cm")
        if not dbh_cm or dbh_cm <= 0:
            continue
        # Quality gate: skip unsupported / low-confidence DBH fits.
        if d.get("status") == "dbh_unsupported":
            continue
        conf = d.get("confidence")
        if conf is not None and conf < args.min_confidence:
            continue

        ba = basal_area_m2(dbh_cm)
        vol = None
        kind = None
        if tid in prof and len(prof[tid]) >= 2:
            vol = merch_volume_from_profile(prof[tid])
            if vol and vol > 0:
                kind = "profile"
        if vol is None or vol <= 0:
            h = d.get("height_m")
            if h and h > 0:
                vol = form_factor_volume(dbh_cm, h, fallback_ff)
                kind = "form_factor"
        if vol and vol > 0:
            t = tariff_from_volume(vol, ba)
            if t is not None and not math.isnan(t) and t > 0:
                sample_tariffs.append(t)
                sample_t_by_tid[tid] = t
                sample_kind[tid] = kind
                sample_vol[tid] = vol

    if not sample_tariffs:
        raise SystemExit("tariff.py: no volume sample trees "
                         "(need a DBH plus a stem profile or a height).")

    # ---- Step 3: stand tariff = robust mean (fallback / inherited by sparse compartments) ----
    stand_tariff, dropped = _robust_mean_tariff(sample_tariffs, args.no_reject, args.outlier_sd)
    if dropped:
        warnings.append(f"{dropped} sample tree(s) rejected as tariff outliers.")

    n_profile = sum(1 for k in sample_kind.values() if k == "profile")
    n_ff = sum(1 for k in sample_kind.values() if k == "form_factor")
    if n_profile == 0:
        warnings.append("No stem-profile volumes available — tariff derived from form-factor "
                        "volumes only (less accurate).")

    # ---- Step 4: fit DBH~height, impute missing DBHs ----
    both = [(d["height_m"], d["dbh_cm"]) for d in dbh.values()
            if d.get("dbh_cm") and d["dbh_cm"] > 0 and d.get("height_m") and d["height_m"] > 0]
    model = _fit_dbh_height(both)
    if model["type"] == "linear" and (model["r2"] or 0) < 0.3:
        warnings.append(f"Weak DBH~height fit (r2={model['r2']:.2f}); imputed DBHs are rough.")

    # ---- Step 4b: assign trees to compartments + refit a tariff per compartment ----
    # Each compartment is a stand: refit its own tariff where it has enough volume-sample
    # trees, otherwise inherit the stand tariff (flagged). DBH~height imputation stays
    # stand-level (stable); only the volume-converting tariff is localised.
    comps = _load_compartments(args.compartments)
    tree_comp: Dict[int, Any] = {}                 # tree_id -> compartment id (or None)
    tree_ll: Dict[int, Tuple[float, float]] = {}   # tree_id -> (lon, lat) in WGS84
    if comps:
        ids = [tid for tid in population
               if population[tid].get("x") not in (None, "") and population[tid].get("y") not in (None, "")]
        if ids:
            from rasterio.warp import transform as warp_transform
            xs = [float(population[tid]["x"]) for tid in ids]
            ys = [float(population[tid]["y"]) for tid in ids]
            lons, lats = warp_transform(args.crs, "EPSG:4326", xs, ys)
            for tid, lon, lat in zip(ids, lons, lats):
                tree_ll[tid] = (lon, lat)
                tree_comp[tid] = _assign_compartment(lon, lat, comps)

    comp_samples: Dict[Any, List[float]] = {}
    for tid, t in sample_t_by_tid.items():
        cid = tree_comp.get(tid)
        if cid is not None:
            comp_samples.setdefault(cid, []).append(t)
    comp_tariff: Dict[Any, float] = {}
    comp_tariff_source: Dict[Any, str] = {}
    for c in comps:
        cid = c["id"]
        s = comp_samples.get(cid, [])
        if len(s) >= args.min_compartment_samples:
            comp_tariff[cid], _drop = _robust_mean_tariff(s, args.no_reject, args.outlier_sd)
            comp_tariff_source[cid] = "refit"
        else:
            comp_tariff[cid] = stand_tariff
            comp_tariff_source[cid] = "stand"

    # ---- Step 5: apply tariff to every tree -> per-tree volume + total ----
    rows: List[Dict[str, Any]] = []
    comp_agg: Dict[Any, Dict[str, Any]] = {}
    total_vol = 0.0
    n_measured = n_imputed = n_no_metrics = 0
    measured_dbhs: List[float] = []
    heights: List[float] = []

    for tid in sorted(population):
        p = population[tid]
        h = p.get("height_m")
        if h and h > 0:
            heights.append(h)

        d = dbh.get(tid, {})
        dbh_cm = d.get("dbh_cm")
        if dbh_cm and dbh_cm > 0:
            dbh_source = "measured"
            n_measured += 1
            measured_dbhs.append(dbh_cm)
        elif h and h > 0:
            dbh_cm = _predict_dbh(model, h)
            dbh_source = "imputed"
            n_imputed += 1
        else:
            dbh_cm = model.get("mean_dbh")
            dbh_source = "stand_mean"
            n_no_metrics += 1

        cid = tree_comp.get(tid)
        applied_tariff = comp_tariff.get(cid, stand_tariff)
        ba = basal_area_m2(dbh_cm) if dbh_cm and dbh_cm > 0 else 0.0
        vol = max(0.0, volume_from_tariff(applied_tariff, ba))
        total_vol += vol

        if cid is not None:
            ca = comp_agg.setdefault(cid, {"n": 0, "vol": 0.0, "dbhs": [], "heights": []})
            ca["n"] += 1
            ca["vol"] += vol
            if dbh_source == "measured" and dbh_cm:
                ca["dbhs"].append(dbh_cm)
            if h and h > 0:
                ca["heights"].append(h)

        rows.append({
            "tree_id": tid,
            "x": p.get("x"),
            "y": p.get("y"),
            "height_m": round(h, 2) if h else "",
            "dbh_cm": round(dbh_cm, 1) if dbh_cm else "",
            "dbh_source": dbh_source,
            "basal_area_m2": round(ba, 5),
            "compartment_id": cid if cid is not None else "",
            "tariff_applied": round(applied_tariff, 2),
            "is_sample_tree": tid in sample_kind,
            "sample_volume_m3": round(sample_vol[tid], 4) if tid in sample_vol else "",
            "merch_volume_m3": round(vol, 4),
        })

    if n_no_metrics:
        warnings.append(f"{n_no_metrics} tree(s) had neither DBH nor height — assigned stand-mean DBH.")
    if len(sample_tariffs) < 4:
        warnings.append(f"Only {len(sample_tariffs)} volume sample tree(s) — tariff is indicative. Aim for 13+.")

    n_total = len(population)
    confidence = ("high" if len(sample_tariffs) >= 13 else
                  "medium" if len(sample_tariffs) >= 4 else "low")

    # ---- write per-tree CSV (table role) ----
    csv_fields = ["tree_id", "x", "y", "height_m", "dbh_cm", "dbh_source", "basal_area_m2",
                  "compartment_id", "tariff_applied", "is_sample_tree", "sample_volume_m3", "merch_volume_m3"]
    with open(args.out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=csv_fields)
        w.writeheader()
        w.writerows(rows)

    # ---- per-compartment tariff + volume (the stand split into its constituent stands) ----
    comp_rows: List[Dict[str, Any]] = []
    by_id = {c["id"]: c for c in comps}
    for cid in sorted(comp_agg, key=lambda v: str(v)):
        ca = comp_agg[cid]
        c = by_id.get(cid, {})
        n_samp = len(comp_samples.get(cid, []))
        area = c.get("area_hectares")
        c_conf = ("high" if n_samp >= 13 else "medium" if n_samp >= args.min_compartment_samples else "inherited")
        comp_rows.append({
            "compartment_id": cid,
            "ref": c.get("ref", str(cid)),
            "tree_count": ca["n"],
            "sample_trees": n_samp,
            "tariff": round(comp_tariff.get(cid, stand_tariff), 2),
            "tariff_rounded": int(round(comp_tariff.get(cid, stand_tariff))),
            "tariff_source": comp_tariff_source.get(cid, "stand"),
            "confidence": c_conf,
            "mean_dbh_cm": round(statistics.fmean(ca["dbhs"]), 1) if ca["dbhs"] else None,
            "mean_height_m": round(statistics.fmean(ca["heights"]), 1) if ca["heights"] else None,
            "merch_volume_m3": round(ca["vol"], 2),
            "area_hectares": round(area, 3) if area else None,
            "volume_per_ha_m3": round(ca["vol"] / area, 1) if area and area > 0 else None,
        })

    n_unassigned = sum(1 for tid in population if tree_comp.get(tid) is None) if comps else 0
    if comps and n_unassigned:
        warnings.append(f"{n_unassigned} tree(s) fell outside every compartment boundary "
                        f"(counted in the stand total, not in any compartment).")

    if args.out_compartments and comp_rows:
        cfields = ["compartment_id", "ref", "tree_count", "sample_trees", "tariff", "tariff_rounded",
                   "tariff_source", "confidence", "mean_dbh_cm", "mean_height_m",
                   "merch_volume_m3", "area_hectares", "volume_per_ha_m3"]
        with open(args.out_compartments, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cfields)
            w.writeheader()
            w.writerows(comp_rows)

    # ---- write headline summary JSON (summary role -> stat cards) ----
    summary = {
        "stand_tariff": round(stand_tariff, 2),
        "stand_tariff_rounded": int(round(stand_tariff)),
        "confidence": confidence,
        "tree_count": n_total,
        "volume_sample_trees": len(sample_tariffs),
        "sample_trees_from_profile": n_profile,
        "sample_trees_from_form_factor": n_ff,
        "dbh_measured": n_measured,
        "dbh_imputed": n_imputed,
        "dbh_from_stand_mean": n_no_metrics,
        "mean_dbh_cm": round(statistics.fmean(measured_dbhs), 1) if measured_dbhs else None,
        "mean_height_m": round(statistics.fmean(heights), 1) if heights else None,
        "tariff_min": round(min(sample_tariffs), 1),
        "tariff_max": round(max(sample_tariffs), 1),
        "total_merch_volume_m3": round(total_vol, 2),
        "mean_volume_per_tree_m3": round(total_vol / n_total, 4) if n_total else None,
        "dbh_height_r2": round(model["r2"], 3) if model.get("r2") is not None else None,
        "compartments_summarised": len(comp_rows),
        "compartments_refit": sum(1 for r in comp_rows if r["tariff_source"] == "refit"),
        "compartments": comp_rows,
        "method": "UK Forestry Commission tariff system (Booklet 36/39), merch volume to 7 cm top",
        "disclaimer": "Field estimate only. Not a formal certified mensuration result.",
        "warnings": warnings,
    }
    with open(args.out_summary, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    # ---- optional: per-tree merch-volume points (WGS84) for the map + per-compartment rollup ----
    if args.out_geojson:
        pts = [r for r in rows if r.get("x") not in (None, "") and r.get("y") not in (None, "")]
        feats = []
        if pts:
            need = [r for r in pts if r["tree_id"] not in tree_ll]
            if need:
                from rasterio.warp import transform as warp_transform
                nx = [float(r["x"]) for r in need]
                ny = [float(r["y"]) for r in need]
                nlon, nlat = warp_transform(args.crs, "EPSG:4326", nx, ny)
                for r, lo, la in zip(need, nlon, nlat):
                    tree_ll[r["tree_id"]] = (lo, la)
            for r in pts:
                lon, lat = tree_ll[r["tree_id"]]
                cid = tree_comp.get(r["tree_id"])
                feats.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {
                        "tree_id": r["tree_id"],
                        "merch_volume_m3": r["merch_volume_m3"],
                        "dbh_cm": r["dbh_cm"],
                        "height_m": r["height_m"],
                        "dbh_source": r["dbh_source"],
                        "compartment_id": cid if cid is not None else "",
                        "is_sample_tree": r["is_sample_tree"],
                    },
                })
        with open(args.out_geojson, "w", encoding="utf-8") as fh:
            json.dump({"type": "FeatureCollection", "features": feats}, fh)

    print(f"tariff.py: stand tariff {summary['stand_tariff_rounded']} "
          f"({summary['stand_tariff']}), {n_total} trees, "
          f"{summary['total_merch_volume_m3']} m3 merch; "
          f"{len(sample_tariffs)} sample trees ({n_profile} profile / {n_ff} form-factor).")
    for wmsg in warnings:
        print(f"  ! {wmsg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
