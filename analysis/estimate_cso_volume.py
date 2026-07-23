"""
estimate_cso_volume.py — Attempt to decode DEP's CSO volume formula.

DEP's CSO advisory type publishes 6 coefficients per waterbody (the same
shape as the old ArcGIS layer's A_Curve..F_Curve fields) plus a rainfall
threshold (0.1in, uniform), a peak-intensity threshold (0.05 in/hr,
uniform), a volume threshold (0.5, uniform) and a 4-hour storm-clustering
gap. The equation combining the 6 coefficients has never been published.

This script cross-references every logged CSO `volume` reading against
the rainfall record (depth-so-far and peak-so-far in the current storm,
using CSO's own 4h gap rule) and scores a handful of *candidate* formula
shapes using DEP's own published coefficients (zero free parameters —
this is a falsification test, not a fit). It also reports the best
achievable fit if the 6 numbers were free parameters, to separate
"wrong shape" from "right shape, coefficients mean something else."

Usage:  python analysis/estimate_cso_volume.py
Writes:
  analysis/cso_volume_estimates.csv — full current snapshot, every
      observation + all candidate predictions (overwritten each run;
      always reflects everything logged so far, not a diff).
  analysis/cso_estimator_log.csv — one row APPENDED each run: how many
      observations existed at that point and how well each candidate
      formula scored. Run this periodically as advisory_log grows to
      watch whether R2 for a candidate climbs/stabilizes (shape looks
      right) or wanders (shape's probably wrong / undecided still).
"""
import csv
import json
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path
from scipy.optimize import curve_fit

ROOT = Path(__file__).resolve().parent.parent
GAP_CSO = 4  # DEP's rainEventGap for the CSO advisory type
LOG_COLS = ["run_time_utc", "n_observations", "n_waterbodies"]
for _name in ("quadratic_depth_peak", "poly5_depth"):
    LOG_COLS += [f"{_name}_r2", f"{_name}_rmse"]


def append(path, cols, rows):
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerows(rows)


def load_cso_params():
    cfg = json.loads((ROOT / "data" / "model_config" / "latest.json").read_text())
    rows = []
    for wb in cfg:
        cso = next((a for a in wb.get("advisoryTypes", [])
                    if a.get("shortName") == "CSO"), None)
        if not cso or not any(cso.get("coefficients", [])):
            continue
        a, b, c, d, e, f = cso["coefficients"]
        rows.append(dict(waterbody=wb["name"], a=a, b=b, c=c, d=d, e=e, f=f))
    return pd.DataFrame(rows)


def load_gauge_map():
    logs = sorted((ROOT / "data").glob("waterbody_log_*.csv"))
    wb = pd.concat(pd.read_csv(f) for f in logs)
    wb = wb.drop_duplicates(subset=["waterbody"], keep="last")
    return wb.set_index("waterbody")["sensor_id"].to_dict()


def load_rain_panel():
    series = {}
    for f in (ROOT / "data" / "rain_history").glob("*.csv"):
        s = pd.read_csv(f, parse_dates=["occurred_on"]).set_index(
            "occurred_on")["precip_in"].sort_index()
        series[f.stem] = s[~s.index.duplicated()]
    return pd.DataFrame(series)


def depth_peak_so_far(gauge_series, at_time):
    """Depth and peak of the storm-in-progress at `at_time`, using the
    CSO 4h gap rule, counting only rain up to and including at_time."""
    window = gauge_series.loc[:at_time]
    rainy = window[window > 0]
    if rainy.empty:
        return 0.0, 0.0
    gaps = rainy.index.to_series().diff() > pd.Timedelta(hours=GAP_CSO)
    storm_id = gaps.cumsum()
    current = rainy[storm_id == storm_id.iloc[-1]]
    return current.sum(), current.max()


CANDIDATES = {
    # Full quadratic surface in (depth, peak) -- 6 terms, matches 6 coeffs
    "quadratic_depth_peak": lambda p, d, k: (
        p.a + p.b * d + p.c * d**2 + p.d_ * k + p.e * k**2 + p.f * d * k),
    # Degree-5 polynomial in depth alone (matches "A_Curve..F_Curve" naming)
    "poly5_depth": lambda p, d, k: (
        p.a + p.b * d + p.c * d**2 + p.d_ * d**3 + p.e * d**4 + p.f * d**5),
}


def score(pred, actual):
    resid = actual - pred
    ss_res = np.sum(resid**2)
    ss_tot = np.sum((actual - actual.mean())**2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    rmse = np.sqrt(np.mean(resid**2))
    return r2, rmse


def main():
    params = load_cso_params()
    gauge_of = load_gauge_map()
    panel = load_rain_panel()

    adv_logs = sorted((ROOT / "data").glob("advisory_log_*.csv"))
    adv = pd.concat(pd.read_csv(f) for f in adv_logs)
    cso = adv[adv.advisory_type == "CSO"].copy()
    cso["occurred_on"] = pd.to_datetime(cso.occurred_on)
    cso = cso[cso.volume > 0]
    cso = cso.drop_duplicates(subset=["waterbody", "occurred_on"])

    rows = []
    for _, r in cso.iterrows():
        gauge = gauge_of.get(r.waterbody)
        if gauge not in panel.columns:
            continue
        depth, peak = depth_peak_so_far(panel[gauge], r.occurred_on)
        rows.append(dict(waterbody=r.waterbody, occurred_on=r.occurred_on,
                          depth=depth, peak=peak, volume=r.volume))
    obs = pd.DataFrame(rows).merge(params, on="waterbody", how="inner")
    obs = obs.rename(columns={"d": "d_"})  # avoid clobbering the 'd' coeff name
    obs["d"] = obs["depth"]
    obs["k"] = obs["peak"]

    print(f"Observations usable: {len(obs)} across {obs.waterbody.nunique()} waterbodies\n")

    print("=== Zero-free-parameter test (DEP's published coefficients as-is) ===")
    log_row = {
        "run_time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_observations": len(obs),
        "n_waterbodies": obs.waterbody.nunique(),
    }
    for name, fn in CANDIDATES.items():
        pred = fn(obs, obs.d, obs.k)
        r2, rmse = score(pred, obs.volume)
        obs[f"pred_{name}"] = pred
        log_row[f"{name}_r2"] = round(r2, 4)
        log_row[f"{name}_rmse"] = round(rmse, 4)
        print(f"{name:28s} R2={r2:7.3f}  RMSE={rmse:8.3f}")

    log_path = ROOT / "analysis" / "cso_estimator_log.csv"
    append(log_path, LOG_COLS, [log_row])
    print(f"Appended run to {log_path}")

    print("\n=== Best achievable fit per waterbody if shape is right but ===")
    print("=== coefficients are refit freely (needs >=7 points/waterbody) ===")
    shapes = {
        "quadratic_depth_peak": lambda X, a, b, c, d_, e, f: (
            a + b * X[0] + c * X[0]**2 + d_ * X[1] + e * X[1]**2 + f * X[0] * X[1]),
        "poly5_depth": lambda X, a, b, c, d_, e, f: (
            a + b * X[0] + c * X[0]**2 + d_ * X[0]**3 + e * X[0]**4 + f * X[0]**5),
    }
    for wbname, g in obs.groupby("waterbody"):
        if len(g) < 7:
            continue
        for shape_name, fn in shapes.items():
            try:
                popt, _ = curve_fit(fn, (g.d.values, g.k.values), g.volume.values, maxfev=5000)
                pred = fn((g.d.values, g.k.values), *popt)
                r2, rmse = score(pred, g.volume.values)
                print(f"{wbname:35s} {shape_name:22s} n={len(g):3d} R2={r2:7.3f} RMSE={rmse:7.3f}")
            except RuntimeError:
                print(f"{wbname:35s} {shape_name:22s} n={len(g):3d}  fit failed to converge")

    out = ROOT / "analysis" / "cso_volume_estimates.csv"
    obs.drop(columns=["d_"]).to_csv(out, index=False)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
