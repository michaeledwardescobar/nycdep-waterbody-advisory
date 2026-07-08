"""
reconstruct_newtown.py — Reconstruct historical waterbody advisories for
Newtown Creek from DEP's own hourly rain gauge record + DEP's own model
parameters (harvested from the advisory API).

Model (validated 2026-07-07 against DEP's live advisory to the exact hour):
  1. Storm events = rainy hours separated by < 6 dry hours.
  2. An event triggers a WQ advisory if depth >= threshold
     (Upper: 0.23 in, Lower: 0.30 in) and peak >= 0.05 in/hr.
  3. Advisory runs from the trigger until
     [last rainy hour of the event] + a * depth^b hours
     (Upper: a=49.45, b=0.78; Lower: a=28.56, b=0.91).
  4. Overlapping advisories merge into single episodes.

Data QC baked in (verified against neighbor gauges BB/WI/RH):
  - five isolated impossible spikes zeroed (telemetry glitches)
  - two dead-gauge windows excluded: 2021-06-09..2022-02-01 and
    2023-02-18..2023-08-18 (gauge recorded 0 while neighbors got 25-43")

Usage:  python analysis/reconstruct_newtown.py
Writes: analysis/newtown_{upper,lower}_episodes.csv + prints summary.
"""
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAIN = ROOT / "data" / "rain_history" / "NC.csv"
GAP = 6
GLITCHES = ["2022-03-31 09:00", "2022-04-07 03:00", "2022-04-20 19:00",
            "2022-05-30 07:00", "2024-03-24 02:00"]
DEAD = [(pd.Timestamp("2021-06-09"), pd.Timestamp("2022-02-01")),
        (pd.Timestamp("2023-02-18"), pd.Timestamp("2023-08-18"))]
MODELS = {"Upper": dict(thr=0.23, a=49.45, b=0.78),
          "Lower": dict(thr=0.30, a=28.56, b=0.91)}


def main():
    nc = pd.read_csv(RAIN, parse_dates=["occurred_on"]).set_index(
        "occurred_on")["precip_in"].sort_index()
    for t in GLITCHES:
        if t in nc.index:
            nc.loc[t] = 0.0

    r = nc[nc > 0]
    eid = (r.index.to_series().diff() >= pd.Timedelta(hours=GAP)).cumsum()
    ev = r.groupby(eid).agg(
        start=lambda x: x.index.min(), end=lambda x: x.index.max(),
        depth="sum", peak="max").reset_index(drop=True)
    ev = ev[~ev.apply(lambda e: any(a <= e.start <= b or a <= e.end <= b
                                    for a, b in DEAD), axis=1)]
    print(f"{len(ev)} storm events in reliable periods")

    for name, m in MODELS.items():
        trig = ev[(ev.depth >= m["thr"]) & (ev.peak >= 0.05)].copy()
        trig["adv_until"] = trig.end + pd.to_timedelta(
            (m["a"] * trig.depth ** m["b"]).round(), unit="h")
        merged = []
        for s, e in sorted(zip(trig.start, trig.adv_until)):
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        ep = pd.DataFrame(merged, columns=["start", "until"])
        ep["dur_hr"] = (ep.until - ep.start).dt.total_seconds() / 3600
        out = ROOT / "analysis" / f"newtown_{name.lower()}_episodes.csv"
        out.parent.mkdir(exist_ok=True)
        ep.to_csv(out, index=False)
        yr = ep.groupby(ep.start.dt.year)["dur_hr"].agg(["count", "sum"])
        print(f"\n=== {name}: {len(trig)} triggering storms, "
              f"{len(ep)} episodes ===")
        print(yr.rename(columns={"count": "episodes",
                                 "sum": "adv_hours"}).to_string())


if __name__ == "__main__":
    main()
