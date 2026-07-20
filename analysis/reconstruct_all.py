"""
reconstruct_all.py — Reconstruct WQ advisory histories for ALL waterbodies
from DEP gauge records + DEP model parameters. Generalizes the Newtown
Creek method:

  QC (automatic, per gauge, cross-validated against the other 13):
    - spike glitches: hours > 3.2 in while other gauges are dry -> zeroed
    - dead windows: 30-day sum ~0 while others catch > 2 in -> masked
  Events: rainy hours separated by <= 6 dry hours (a gap must be MORE
    than 6h to split events; validated against live DEP polls 2026-07);
    events adjacent to masked periods are excluded.
  Advisory: event depth >= waterbody threshold (and peak >= 0.05 in/hr)
    -> advisory until [last rainy hour] + ceil(a * depth^b) hours
    (ceiling, not rounding — validated exact vs. live DEP durations);
    overlaps merged.
  Stats are normalized by each gauge's reliable hours.

Usage:  python analysis/reconstruct_all.py
Writes: analysis/waterbody_summary.csv, analysis/all_waterbody_episodes.csv
"""
import numpy as np
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAIN_DIR = ROOT / "data" / "rain_history"
GAP = 6


def load_params():
    logs = sorted((ROOT / "data").glob("waterbody_log_*.csv"))
    wb = pd.concat(pd.read_csv(f) for f in logs)
    wb = wb.drop_duplicates(subset=["waterbody"], keep="last")
    return wb[["waterbody", "sensor_id", "wq_threshold_in",
               "wq_coeff_a", "wq_coeff_b"]].dropna()


def load_panel():
    series = {}
    for f in RAIN_DIR.glob("*.csv"):
        s = pd.read_csv(f, parse_dates=["occurred_on"]).set_index(
            "occurred_on")["precip_in"].sort_index()
        series[f.stem] = s[~s.index.duplicated()]
    return pd.DataFrame(series)


def qc(panel):
    for g in panel.columns:
        others = panel.drop(columns=g).median(axis=1)
        panel.loc[(panel[g] > 3.2) & (others < 0.02), g] = 0.0
        own30 = panel[g].rolling("30D").sum()
        oth30 = others.rolling("30D").sum()
        dead = (own30 < 0.1) & (oth30 > 2.0)
        dead = dead[::-1].rolling("30D", min_periods=1).max()[::-1].astype(bool)
        panel.loc[dead, g] = np.nan
    return panel


def events_for(s):
    nan_times = s.index[s.isna()]
    r = s[s > 0]
    eid = (r.index.to_series().diff() > pd.Timedelta(hours=GAP)).cumsum()
    ev = r.groupby(eid).agg(start=lambda x: x.index.min(),
                            end=lambda x: x.index.max(),
                            depth="sum", peak="max").reset_index(drop=True)
    if len(nan_times):
        bad = ev.apply(lambda e: (
            (nan_times >= e.start - pd.Timedelta(hours=GAP)) &
            (nan_times <= e.end + pd.Timedelta(hours=GAP))).any(), axis=1)
        ev = ev[~bad]
    return ev


def main():
    wb = load_params()
    panel = qc(load_panel())
    gauge_events = {g: events_for(panel[g]) for g in panel.columns}
    reliable = {g: panel[g].notna().sum() for g in panel.columns}

    rows, episodes = [], []
    for _, w in wb.iterrows():
        ev = gauge_events[w.sensor_id]
        trig = ev[(ev.depth >= w.wq_threshold_in) & (ev.peak >= 0.05)].copy()
        trig["until"] = trig.end + pd.to_timedelta(
            np.ceil(w.wq_coeff_a * trig.depth ** w.wq_coeff_b), unit="h")
        merged = []
        for s, e in sorted(zip(trig.start, trig.until)):
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        ep = pd.DataFrame(merged, columns=["start", "until"])
        if len(ep):
            ep["dur_hr"] = (ep.until - ep.start).dt.total_seconds() / 3600
            ep.insert(0, "waterbody", w.waterbody)
            episodes.append(ep)
        yrs = reliable[w.sensor_id] / 8766
        rows.append(dict(
            waterbody=w.waterbody, gauge=w.sensor_id,
            threshold_in=w.wq_threshold_in,
            episodes_per_yr=round(len(ep) / yrs, 1),
            adv_hours_per_yr=round(ep.dur_hr.sum() / yrs) if len(ep) else 0,
            pct_time=round(100 * ep.dur_hr.sum() / reliable[w.sensor_id], 1)
                     if len(ep) else 0,
            median_dur_hr=round(ep.dur_hr.median()) if len(ep) else 0))

    out = ROOT / "analysis"
    out.mkdir(exist_ok=True)
    pd.DataFrame(rows).sort_values("pct_time", ascending=False).to_csv(
        out / "waterbody_summary.csv", index=False)
    pd.concat(episodes).to_csv(out / "all_waterbody_episodes.csv", index=False)
    print("Wrote waterbody_summary.csv and all_waterbody_episodes.csv")


if __name__ == "__main__":
    main()
