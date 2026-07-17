r"""
predict_coupe.py - HARVEST PLANNING v1. Predict productivity, time and cost
for a new coupe, and export a planning CSV.

Input: a stems CSV for the coupe to quote - same schema as training rows
(needs at minimum: object_id, stem_key, dbh_cm, stem_volume_m3, n_logs,
species, lat, lon). Terrain/canopy/rivers columns are used if present and
left blank (NaN) if the coupe wasn't flown - the model handles both.

  python scripts/predict_coupe.py `
      --model models/v04/model.pkl `
      --stems data/new_coupe_stems.csv `
      --out quotes/new_coupe_quote.csv

Outputs two files:
  <out>              per-coupe summary: volume, m3/PMH0, machine-hours, cost
  <out>.stems.csv    per-stem predicted cycle (for the speed map)

FLEET CONSTANTS (measured from MOM, Jul 2026 - edit if the fleet changes):
  utilisation 0.70, harvester fuel 1.02 L/m3,
  forwarding 16.5 m3/eng-h, forwarding fuel 0.90 L/m3.
Rates (GBP) are placeholders - set --machine-rate / --fuel-price for real quotes.

Every quote carries a HONEST BAND: standing conifer +/-18%, windblow flagged
LOW CONFIDENCE (route to the band, not a point estimate).
"""
from __future__ import annotations
import argparse
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ---- fleet constants (from MOM analysis) -------------------------------
UTILISATION = 0.70          # PMH0 -> engine hours (harvester)
HV_FUEL_L_PER_M3 = 1.02
FWD_M3_PER_ENGINE_H = 16.5
FWD_FUEL_L_PER_M3 = 0.90
STANDING_BAND = 0.18        # +/- fraction
WINDBLOW_DBH = 16.0         # median DBH below this -> windblow low-confidence


def add_coupe_context(df):
    def hull_ha(g):
        latm = 111320.0
        lonm = 111320.0 * np.cos(np.radians(g.lat.mean()))
        return max((g.lat.quantile(.99) - g.lat.quantile(.01)) * latm *
                   (g.lon.quantile(.99) - g.lon.quantile(.01)) * lonm, 1) / 1e4
    agg = df.groupby("object_id").apply(lambda g: pd.Series({
        "c_density": len(g) / hull_ha(g),
        "c_dbh_med": g.dbh_cm.median(),
        "c_vol_med": g.stem_volume_m3.median(),
        "c_canopy_med": g.get("canopy_p95_m_laz", pd.Series(dtype=float)).median(),
        "c_slope_med": g.get("slope_deg_os5", pd.Series(dtype=float)).median(),
        "c_rough_med": g.get("roughness_m_os5", pd.Series(dtype=float)).median(),
    }), include_groups=False)
    return df.merge(agg, on="object_id")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--stems", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--machine-rate", type=float, default=150.0,
                    help="GBP per productive machine hour (harvester+forwarder combined)")
    ap.add_argument("--fuel-price", type=float, default=1.30, help="GBP per litre")
    args = ap.parse_args()

    with open(args.model, "rb") as fh:
        M = pickle.load(fh)
    model, feats, sm, blend_w = (M["model"], M["features"], M["smearing"],
                                 M.get("blend_w", 0.5))

    df = pd.read_csv(args.stems)
    need = {"object_id", "stem_key", "dbh_cm", "stem_volume_m3", "species"}
    miss = need - set(df.columns)
    if miss:
        sys.exit(f"stems missing columns: {miss}")
    df["species_code"] = (df.species == "Spruce").astype(int)
    df["n_logs"] = df.get("n_logs", 3)
    df["hour"] = df.get("hour", 11)
    # canopy_deficit if canopy present, else NaN (model handles)
    if "canopy_p95_m_laz" in df and "canopy_deficit" not in df:
        b = df[df.canopy_p95_m_laz.notna()].copy()
        if len(b):
            b["bin"] = (b.dbh_cm // 2 * 2)
            env = b.groupby("bin").canopy_p95_m_laz.quantile(0.9)
            df["canopy_deficit"] = ((df.dbh_cm // 2 * 2).map(env)
                                    - df.canopy_p95_m_laz).clip(lower=0)
    df = add_coupe_context(df)
    for f in feats:
        if f not in df:
            df[f] = np.nan

    # per-stem cycle: blend model with volume curve (as trained)
    lm = model.predict(df[feats])
    # curve fit on this coupe's own volumes as a within-coupe guardrail
    ok = df.stem_volume_m3 > 0
    b = np.polyfit(np.log(df.loc[ok, "stem_volume_m3"]),
                   lm[ok.values], 1)          # anchor curve to model scale
    lc = np.polyval(b, np.log(df.stem_volume_m3.clip(lower=1e-3)))
    lblend = blend_w * lm + (1 - blend_w) * lc
    df["pred_cycle_s"] = np.exp(lblend) * sm

    # per-coupe rollup
    out_rows = []
    for oid, g in df.groupby("object_id"):
        vol = g.stem_volume_m3.sum()
        cut_h = g.pred_cycle_s.sum() / 3600.0            # PMH0
        pmh0 = vol / cut_h if cut_h else np.nan
        hv_engine_h = cut_h / UTILISATION
        fwd_engine_h = vol / FWD_M3_PER_ENGINE_H
        machine_h = hv_engine_h + fwd_engine_h
        fuel_l = vol * (HV_FUEL_L_PER_M3 + FWD_FUEL_L_PER_M3)
        cost = machine_h * args.machine_rate + fuel_l * args.fuel_price
        windblow = g.dbh_cm.median() < WINDBLOW_DBH
        band = STANDING_BAND if not windblow else 0.30
        out_rows.append({
            "coupe": oid,
            "n_stems": len(g),
            "volume_m3": round(vol, 1),
            "predicted_m3_per_pmh0": round(pmh0, 1),
            "band_low_m3_per_pmh0": round(pmh0 * (1 - band), 1),
            "band_high_m3_per_pmh0": round(pmh0 * (1 + band), 1),
            "harvester_pmh0_h": round(cut_h, 1),
            "harvester_engine_h": round(hv_engine_h, 1),
            "forwarder_engine_h": round(fwd_engine_h, 1),
            "total_machine_h": round(machine_h, 1),
            "fuel_litres": round(fuel_l, 0),
            "est_cost_gbp": round(cost, 0),
            "cost_band_low_gbp": round(cost * (1 - band), 0),
            "cost_band_high_gbp": round(cost * (1 + band), 0),
            "confidence": "LOW (windblow)" if windblow else "standard (+/-18%)",
        })
    summary = pd.DataFrame(out_rows)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(args.out, index=False)
    df[["object_id", "stem_key", "lat", "lon", "dbh_cm", "stem_volume_m3",
        "pred_cycle_s"]].to_csv(str(args.out) + ".stems.csv", index=False)

    print(summary.to_string(index=False))
    print(f"\n-> {args.out}")
    print(f"-> {args.out}.stems.csv  (per-stem, for the speed map)")


if __name__ == "__main__":
    main()
