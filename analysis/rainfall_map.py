#!/usr/bin/env python3
"""
rainfall_map.py — cumulative rainfall map for the NYC DEP WWTP rain gauge network.

Reads a table of gauge readings, aggregates them into a per-gauge time series, and
writes ONE self-contained HTML file. The period is adjustable in the browser: drag a
window across the citywide hyetograph and every gauge total, the map symbols, and the
optional interpolated surface recompute live.

The output has NO external dependencies — no CDN scripts, no tile server, no webfonts.
It renders on a canvas using borough outlines embedded at build time, so it works
offline, inside sandboxed viewers, and on a phone.

Dependencies: pandas, numpy.

Usage
-----
    python rainfall_map.py --input data/rain_gauges.csv --out rainfall_map.html

    # source column is a running/season-to-date total instead of per-interval depth
    python rainfall_map.py --input data/rain_gauges.csv --mode running

    # pin an initial window (still adjustable in the browser)
    python rainfall_map.py --input data/rain_gauges.csv --start 2026-07-01 --end 2026-07-28

    # hourly buckets; surveyed gauge coordinates
    python rainfall_map.py --input data/rain_gauges.csv --freq h --gauges config/gauge_sites.csv

Column names are auto-detected; override with --gauge-col / --time-col / --value-col.
The basemap comes from boroughs.json next to this script (NYC Open Data gthc-hcne,
simplified). If it is missing the map still draws, just without borough outlines.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------------------
# Gauge network
# --------------------------------------------------------------------------------------
# The 14 DEP water resource recovery facilities that host the rainfall gauges.
# Coordinates are plant-centroid approximations good to ~100 m — fine for a citywide
# map, not for siting work. Replace with surveyed gauge positions via --gauges
# (CSV columns: id,name,borough,lat,lon).

GAUGE_SITES = [
    ("26W",  "26th Ward",       "Brooklyn",      40.6396, -73.8770),
    ("BB",   "Bowery Bay",      "Queens",        40.7789, -73.8919),
    ("CI",   "Coney Island",    "Brooklyn",      40.5905, -73.9295),
    ("HP",   "Hunts Point",     "Bronx",         40.7969, -73.8779),
    ("JA",   "Jamaica",         "Queens",        40.6602, -73.7830),
    ("NC",   "Newtown Creek",   "Brooklyn",      40.7360, -73.9420),
    ("NR",   "North River",     "Manhattan",     40.8232, -73.9575),
    ("OB",   "Oakwood Beach",   "Staten Island", 40.5588, -74.1180),
    ("OH",   "Owls Head",       "Brooklyn",      40.6396, -74.0361),
    ("PR",   "Port Richmond",   "Staten Island", 40.6389, -74.1298),
    ("RH",   "Red Hook",        "Brooklyn",      40.7000, -73.9760),
    ("RK",   "Rockaway",        "Queens",        40.5915, -73.7930),
    ("TI",   "Tallman Island",  "Queens",        40.7930, -73.8330),
    ("WI",   "Wards Island",    "Manhattan",     40.7930, -73.9230),
]

GAUGE_ALIASES = {
    "26th ward": "26W", "26w": "26W", "26": "26W", "26 ward": "26W", "twenty sixth ward": "26W",
    "bowery bay": "BB", "bb": "BB",
    "coney island": "CI", "ci": "CI",
    "hunts point": "HP", "hunt's point": "HP", "hp": "HP",
    "jamaica": "JA", "jam": "JA", "ja": "JA",
    "newtown creek": "NC", "newton creek": "NC", "nc": "NC",
    "north river": "NR", "nr": "NR",
    "oakwood beach": "OB", "oakwood": "OB", "ob": "OB",
    "owls head": "OH", "owl's head": "OH", "oh": "OH",
    "port richmond": "PR", "pr": "PR",
    "red hook": "RH", "rh": "RH",
    "rockaway": "RK", "rock": "RK", "rk": "RK",
    "tallman island": "TI", "tallmans island": "TI", "ti": "TI",
    "wards island": "WI", "ward's island": "WI", "wi": "WI",
}

GAUGE_COL_HINTS = ["gauge", "gage", "gauge_id", "station", "station_name", "site",
                   "site_name", "plant", "wwtp", "wrrf", "facility", "location", "name"]
TIME_COL_HINTS = ["datetime", "date_time", "timestamp", "occurred_on", "occurred_at",
                  "reading_time", "obs_time", "observed_at", "recorded_at", "date", "time"]
VALUE_COL_HINTS = ["rainfall_in", "rainfall", "rain_in", "rain", "precip_in",
                   "precipitation", "precip", "depth", "inches", "amount", "value", "total"]


# --------------------------------------------------------------------------------------
# Loading & normalising
# --------------------------------------------------------------------------------------

def _pick_column(columns, hints, explicit=None, kind="column"):
    if explicit:
        if explicit not in columns:
            sys.exit(f"error: --{kind}-col '{explicit}' not found. Available: {list(columns)}")
        return explicit
    lowered = {str(c).strip().lower(): c for c in columns}
    for hint in hints:
        if hint in lowered:
            return lowered[hint]
    for hint in hints:
        for low, orig in lowered.items():
            if hint in low:
                return orig
    if kind == "gauge":
        raise LookupError("no gauge column")
    sys.exit(f"error: could not auto-detect the {kind} column. "
             f"Pass --{kind}-col explicitly. Available: {list(columns)}")


def expand_inputs(tokens) -> list[Path]:
    """Accept files, directories and globs. A directory contributes its *.csv."""
    paths = []
    for tok in tokens:
        p = Path(tok)
        if p.is_dir():
            # skip sidecar/metadata files (_meta.json, .DS_Store) and keep only the
            # dominant tabular extension so one stray file can't derail the run
            cands = [q for q in p.iterdir()
                     if q.is_file() and not q.name.startswith((".", "_"))
                     and q.suffix.lower() in (".csv", ".tsv", ".txt", ".parquet")]
            if not cands:
                sys.exit(f"error: no readings files in {p} (looked for .csv/.tsv/.txt/.parquet, "
                         f"ignoring names starting with '.' or '_')")
            from collections import Counter
            best = Counter(q.suffix.lower() for q in cands).most_common(1)[0][0]
            paths += sorted(q for q in cands if q.suffix.lower() == best)
        elif any(c in str(tok) for c in "*?["):
            paths += sorted(Path().glob(str(tok)))
        else:
            paths.append(p)
    missing = [p for p in paths if not p.exists()]
    if missing:
        sys.exit("error: input not found: " + ", ".join(str(m) for m in missing))
    if not paths:
        sys.exit("error: --input matched no files")
    return paths


def load_many(tokens, gauge_col=None, time_col=None, value_col=None) -> pd.DataFrame:
    """One file with a gauge column, or many files where the FILENAME is the gauge."""
    paths = expand_inputs(tokens)
    frames = []
    for path in paths:
        df = _read_table(path)
        t = _pick_column(df.columns, TIME_COL_HINTS, time_col, "time")
        v = _pick_column(df.columns, VALUE_COL_HINTS, value_col, "value")
        try:
            g = _pick_column(df.columns, GAUGE_COL_HINTS, gauge_col, "gauge")
            labels = df[g]
            if labels.isna().all() or (labels.astype(str).str.strip() == "").all():
                raise LookupError
        except (SystemExit, LookupError):
            if len(paths) == 1 and gauge_col:
                raise
            labels = path.stem            # filename is the gauge id
        out = pd.DataFrame({"gauge_raw": labels, "ts": df[t], "value": df[v]})
        frames.append(out)

    out = pd.concat(frames, ignore_index=True)
    out["ts"] = pd.to_datetime(out["ts"], errors="coerce", format="mixed")
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    out = out.dropna(subset=["ts", "value"])
    if out.empty:
        sys.exit("error: no rows survived timestamp/value parsing — check --time-col and --value-col")
    print(f"  read {len(paths)} file(s), {len(out):,} readings")
    return out


def _read_table(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in (".csv", ".txt", ".tsv"):
        df = pd.read_csv(path, sep=None, engine="python")
    elif suffix in (".json", ".geojson"):
        df = pd.json_normalize(json.loads(path.read_text()))
    elif suffix in (".parquet", ".pq"):
        df = pd.read_parquet(path)
    elif suffix in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    else:
        sys.exit(f"error: unsupported input type '{suffix}'")
    return df


def resolve_gauges(readings: pd.DataFrame, sites: pd.DataFrame) -> pd.DataFrame:
    known = {str(i).strip().lower(): str(i) for i in sites["id"]}
    known.update({str(n).strip().lower(): str(i) for i, n in zip(sites["id"], sites["name"])})
    known.update(GAUGE_ALIASES)

    def match(raw):
        key = str(raw).strip().lower()
        if key in known:
            return known[key]
        key2 = key.replace("wwtp", "").replace("wrrf", "").replace("plant", "").strip(" -_,")
        return known.get(key2)

    readings = readings.copy()
    readings["gauge"] = readings["gauge_raw"].map(match)
    unmatched = sorted(readings.loc[readings["gauge"].isna(), "gauge_raw"].astype(str).unique())
    if unmatched:
        print(f"  ! {len(unmatched)} unmatched gauge label(s), dropped: {', '.join(unmatched[:8])}"
              + (" ..." if len(unmatched) > 8 else ""), file=sys.stderr)
        print("    Add them to --gauges (id,name,borough,lat,lon) to include them.", file=sys.stderr)
    return readings.dropna(subset=["gauge"])


def to_increments(readings: pd.DataFrame, mode: str) -> pd.DataFrame:
    """Per-reading incremental depth. 'running' sources are differenced per gauge, with
    negative steps treated as counter resets."""
    if mode == "incremental":
        readings = readings.copy()
        readings["inc"] = readings["value"].clip(lower=0)
        return readings
    readings = readings.sort_values(["gauge", "ts"]).copy()
    step = readings.groupby("gauge")["value"].diff()
    readings["inc"] = np.where(step.isna() | (step < 0), 0.0, step)
    return readings


def mark_spikes(readings: pd.DataFrame, max_rate: float,
                suspect_rate: float, corroboration: float) -> pd.DataFrame:
    """Flag sensor faults using the network as a witness, not a fixed ceiling.

    A fixed in/hr cap can't tell a stuck tipping bucket from a real cloudburst over
    one neighbourhood — and clipping genuine short-duration extremes is exactly the
    wrong failure for a stormwater programme. So two tests:

      1. Above `max_rate` the reading is physically impossible anywhere and is
         always dropped. NYC's highest observed hourly rate is about 3.2 in/hr
         (Ida, 1 Sep 2021), so the 8 in/hr default has enormous headroom.
      2. Above `suspect_rate`, the reading is only dropped if NO other gauge in the
         network caught at least `corroboration` of it in the same period. A real
         convective cell over one sewershed still registers at its neighbours; a
         faulty bucket spikes alone.

    Rate is computed against each gauge's own median sampling interval, so this
    works for hourly or 15-minute feeds alike.
    """
    readings = readings.sort_values(["gauge", "ts"]).copy()
    gap_h = readings.groupby("gauge")["ts"].diff().dt.total_seconds() / 3600
    interval = gap_h.groupby(readings["gauge"]).transform("median").fillna(1.0).clip(lower=1/60)
    readings["rate"] = readings["inc"] / interval

    # highest rate at any OTHER gauge in the same period
    piv = readings.pivot_table(index="ts", columns="gauge", values="rate", aggfunc="max")
    v = np.nan_to_num(piv.to_numpy(), nan=-1.0)
    part = np.sort(v, axis=1)
    top1 = part[:, -1]
    top2 = part[:, -2] if part.shape[1] > 1 else np.full_like(top1, -1.0)
    peer = np.where(v >= top1[:, None], top2[:, None], top1[:, None])
    peer_df = pd.DataFrame(peer, index=piv.index, columns=piv.columns).clip(lower=0.0)
    readings["peer"] = peer_df.stack().reindex(
        pd.MultiIndex.from_arrays([readings["ts"], readings["gauge"]])).to_numpy()

    hard = readings["rate"] > max_rate
    isolated = ((readings["rate"] > suspect_rate)
                & (readings["peer"] < corroboration * readings["rate"]))
    readings["spike"] = hard | isolated

    n = int(readings["spike"].sum())
    if n:
        worst = readings.loc[readings["spike"]].nlargest(1, "rate").iloc[0]
        kept = readings[(readings["rate"] > suspect_rate) & ~readings["spike"]]
        print(f"  ! {n} reading(s) dropped as sensor faults "
              f"(worst: {worst['gauge']} {worst['ts']:%Y-%m-%d %H:%M} = {worst['inc']:.2f} in, "
              f"best peer {worst['peer']:.2f})", file=sys.stderr)
        if len(kept):
            k = kept.nlargest(1, "rate").iloc[0]
            print(f"    kept {len(kept)} corroborated high-intensity reading(s), "
                  f"peak {k['gauge']} {k['ts']:%Y-%m-%d %H:%M} = {k['inc']:.2f} in "
                  f"(peer {k['peer']:.2f})", file=sys.stderr)
    return readings


def mark_offline(series: pd.DataFrame, window: int, ratio: float, floor: float) -> pd.DataFrame:
    """Flag spans where a gauge reports far less than the rest of the network.

    Uses the network itself as the reference rather than a fixed dry-spell length,
    so a real citywide drought doesn't get mistaken for an outage: a gauge is only
    suspect when everyone else is catching rain and it isn't.
    """
    minp = max(3, window // 3)
    net = series.median(axis=1).rolling(window, min_periods=minp).sum()
    caught = series.rolling(window, min_periods=minp).sum()
    # `DataFrame & Series` aligns on COLUMNS, so broadcast the floor down axis 0 explicitly
    below = caught.lt(net * ratio, axis=0)
    meaningful = below.mul(net.ge(floor).astype(int), axis=0).astype(bool)
    # each flagged day summarises the window behind it — widen the flag to cover it
    wide = (meaningful.astype(float).iloc[::-1]
            .rolling(window, min_periods=1).max().iloc[::-1])
    return wide.fillna(0.0).astype(bool)


def build_series(readings: pd.DataFrame, freq: str):
    idx = readings.set_index("ts")
    clean = idx.assign(v=idx["inc"].where(~idx["spike"], 0.0))
    binned = (clean.groupby("gauge")["v"].resample(freq).sum()
              .unstack(0).fillna(0.0).sort_index())
    spiked = (idx.groupby("gauge")["spike"].resample(freq).max()
              .unstack(0).reindex(binned.index).fillna(False).astype(bool))
    fmt = "%Y-%m-%d" if freq.lower().startswith("d") else "%Y-%m-%d %H:00"
    labels = [d.strftime(fmt) for d in binned.index]
    return labels, binned, spiked


# --------------------------------------------------------------------------------------
# HTML output — single canvas, zero external requests
# --------------------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>__TITLE__</title>
<style>
  :root{
    --ink:#12202b;
    --paper:#eef1f0;
    --panel:#ffffff;
    --rule:#c9d2d3;
    --water:#dde5e6;
    --land:#f7f8f7;
    --alert:#c2532b;
    --mono:ui-monospace,"SF Mono","Cascadia Mono","Roboto Mono",Menlo,Consolas,monospace;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;overscroll-behavior:none}
  body{background:var(--paper);color:var(--ink);font:15px/1.45 var(--sans);
       display:flex;flex-direction:column}

  header{padding:12px 18px 10px;border-bottom:1px solid var(--rule);background:var(--panel);
         display:flex;flex-wrap:wrap;gap:8px 26px;align-items:baseline}
  h1{margin:0;font-size:16px;font-weight:600;letter-spacing:-0.01em}
  .eyebrow{font:500 10px/1.3 var(--mono);letter-spacing:.13em;text-transform:uppercase;
           color:var(--alert);display:block;margin-bottom:5px}
  .srcline{display:block;margin-top:5px;font-size:11.5px;line-height:1.45;color:#7b8b92;
           max-width:62ch}

  main{flex:1;display:flex;min-height:0}
  .tabs{display:none;gap:0;border-bottom:1px solid var(--rule);background:var(--panel)}
  .tab{flex:1;border:0;border-bottom:2px solid transparent;border-radius:0;padding:9px 0;
       font:500 12.5px/1 var(--sans);color:#7b8b92;background:none}
  .tab.on{color:var(--ink);border-bottom-color:var(--alert)}
  .mapwrap{flex:1;min-width:0;position:relative;background:var(--water)}
  #map{width:100%;height:100%;display:block;touch-action:none;cursor:grab}
  #map.dragging{cursor:grabbing}
  .legend{position:absolute;left:12px;top:12px;background:rgba(255,255,255,.94);
          border:1px solid var(--rule);border-radius:3px;padding:8px 10px;
          font:400 10.5px/1.4 var(--mono);color:#40545e;pointer-events:none}
  .ramp{height:8px;width:132px;margin:5px 0 3px;border-radius:1px}
  .rampends{display:flex;justify-content:space-between}
  .mapbtns{position:absolute;right:12px;top:12px;display:flex;flex-direction:column;gap:6px}
  .mapbtns button{width:32px;height:32px;padding:0;font-size:15px;line-height:1}

  aside{width:284px;flex:none;background:var(--panel);border-left:1px solid var(--rule);
        overflow-y:auto;padding:12px 0 18px}
  aside h2{margin:0 16px 9px;font:500 10px/1 var(--mono);letter-spacing:.15em;
           text-transform:uppercase;color:#5c6f78}
  table{width:100%;border-collapse:collapse;font-size:13px}
  td{padding:5px 16px;border-bottom:1px solid #eef1f0;vertical-align:middle}
  td.total{text-align:right;font:500 13px/1 var(--mono);white-space:nowrap}
  td.bar{width:62px;padding-left:0;padding-right:8px}
  .barfill{height:7px;border-radius:1px;min-width:1px}
  tr.sel{background:#fdf3ef}
  .sub{font:400 10px/1.3 var(--mono);color:#8a999f}
  .note{padding:11px 16px 0;font-size:11.5px;line-height:1.5;color:#7b8b92}

  .controls{border-top:1px solid var(--rule);background:var(--panel);
            padding:9px 18px 12px;display:flex;gap:16px;align-items:flex-end;flex-wrap:wrap}
  .brushwrap{flex:1;min-width:240px}
  .brushlabel{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px;
              font:400 10.5px/1 var(--mono);color:#5c6f78;gap:10px}
  .brushlabel strong{font-weight:600;color:var(--ink);font-size:11.5px}
  .dates{display:flex;align-items:center;gap:5px;flex:none;color:#8a999f}
  .dates input{font:400 11px/1 var(--mono);color:var(--ink);background:var(--panel);
               border:1px solid var(--rule);border-radius:3px;padding:4px 5px;min-width:0}
  .dates input:hover{border-color:var(--ink)}
  #hyeto{width:100%;height:60px;display:block;cursor:col-resize;touch-action:none}
  .chips{display:flex;gap:6px;overflow-x:auto;padding:0 0 6px;scrollbar-width:thin;
         -webkit-overflow-scrolling:touch}
  .chip{flex:none;border:1px solid var(--rule);border-radius:11px;padding:3px 9px;
        background:var(--panel);cursor:pointer;white-space:nowrap;
        font:400 11px/1.35 var(--sans);color:#40545e}
  .chip:hover{border-color:var(--ink)}
  .chip.on{border-color:var(--alert);background:#fdf3ef;color:var(--ink)}
  .chip b{font-weight:600}
  .chip .d{font:500 10.5px/1 var(--mono);color:#8a999f;margin-left:5px}
  .chip.on .d{color:var(--alert)}
  .opts{display:flex;gap:14px;align-items:center;flex-wrap:wrap;font-size:12.5px;color:#40545e}
  .opts label{display:flex;gap:6px;align-items:center;cursor:pointer;user-select:none}
  button{font:500 12px/1 var(--sans);color:var(--ink);background:var(--panel);
         border:1px solid var(--rule);border-radius:3px;padding:7px 10px;cursor:pointer}
  button:hover{border-color:var(--ink)}
  :focus-visible{outline:2px solid var(--alert);outline-offset:2px}

  @media (max-width:860px){
    /* one pane at a time — a map and a 14-row table can't both be usable on a phone.
       Both panes flex to fill whatever the header and controls leave, so nothing overlaps. */
    .tabs{display:flex}
    main{flex-direction:column}
    .mapwrap, aside{flex:1;min-height:0;width:100%;height:auto}
    aside{border-left:0;max-height:none}
    body.pane-map aside{display:none}
    body.pane-table .mapwrap{display:none}

    header{padding:9px 14px 7px}
    h1{font-size:14.5px}
    .srcline{display:none}
    .controls{padding:7px 14px 10px;gap:8px}
    #hyeto{height:46px}
    .brushlabel{flex-wrap:wrap;gap:4px 10px}
    .dates{margin-left:auto}
    .opts{flex-wrap:nowrap;overflow-x:auto;gap:12px;padding-bottom:2px;width:100%}
    .opts label, .opts button{flex:none}
    button{padding:6px 9px}
    aside h2{margin-bottom:6px}
    td{padding:7px 14px}
    .legend{font-size:10px;padding:6px 8px}
    .ramp{width:104px}
  }
</style>
</head>
<body>
<header>
  <div>
    <span class="eyebrow">Independent project &middot; not affiliated with, endorsed by, or produced for NYC DEP</span>
    <h1>Cumulative rainfall across the NYC DEP rain gauge network</h1>
    <span class="srcline">Built from publicly available gauge readings at the 14 water resource
      recovery facilities. Quality filtering and interpretation are the author's own.</span>
  </div>
</header>

<div class="tabs" id="tabs">
  <button class="tab on" data-pane="map">Map</button>
  <button class="tab" data-pane="table">Gauge totals</button>
</div>

<main>
  <div class="mapwrap">
    <canvas id="map"></canvas>
    <div class="mapbtns">
      <button id="zin" title="Zoom in">+</button>
      <button id="zout" title="Zoom out">&minus;</button>
      <button id="zfit" title="Reset view" style="font-size:11px">fit</button>
    </div>
    <div class="legend">
      <div>Rainfall depth (in)</div>
      <div class="ramp" id="lg-ramp"></div>
      <div class="rampends"><span>0</span><span id="lg-hi">&mdash;</span></div>
      <div style="margin-top:5px">Circle area &prop; depth</div>
      <div style="margin-top:3px;color:#c2532b">dashed = suspect data</div>
    </div>
  </div>
  <aside>
    <h2>Gauge totals (in)</h2>
    <p id="qa" style="margin:0 16px 8px;font:400 11px/1.4 var(--mono);color:#c2532b"></p>
    <table><tbody id="ranked"></tbody></table>
    <p class="note">Totals are the sum of gauge increments inside the selected window.
    Drag across the hyetograph to change the period, or drag the shaded band to slide it.</p>
  </aside>
</main>

<div class="controls">
  <div class="brushwrap">
    <div class="brushlabel">
      <span>Peak gauge depth per day &middot; <strong id="brush-readout">&mdash;</strong></span>
      <span class="dates"><input type="date" id="d0"><span>&rarr;</span><input type="date" id="d1"></span>
    </div>
    <div id="events" class="chips"></div>
    <canvas id="hyeto"></canvas>
  </div>
  <div class="opts">
    <label><input type="checkbox" id="opt-labels" checked> Labels</label>
    <label><input type="checkbox" id="opt-raw"> Unfiltered</label>
    <button id="btn-all">Full record</button>
    <button id="btn-csv">Download window CSV</button>
  </div>
</div>

<script>
"use strict";
const DATA = __PAYLOAD__;
const N = DATA.periods.length;
let i0 = DATA.initial[0], i1 = DATA.initial[1];
let selected = null;

/* ---------- colour ramp: dry sand -> gauge teal -> saturated indigo ---------- */
const STOPS = [[232,226,209],[63,154,156],[27,47,122]];
function ramp(t){
  t = Math.max(0, Math.min(1, t || 0));
  const s = t * (STOPS.length - 1), i = Math.min(Math.floor(s), STOPS.length - 2), f = s - i;
  const a = STOPS[i], b = STOPS[i+1];
  return [0,1,2].map(k => Math.round(a[k] + (b[k]-a[k])*f));
}
const rgb = c => `rgb(${c[0]},${c[1]},${c[2]})`;
document.getElementById('lg-ramp').style.background =
  `linear-gradient(90deg,${rgb(STOPS[0])},${rgb(STOPS[1])},${rgb(STOPS[2])})`;

/* ---------- projection: equirectangular, x scaled by cos(mid-latitude) ---------- */
const KX = Math.cos(40.71 * Math.PI / 180);
const px_ = lon => lon * KX;
const py_ = lat => -lat;

/* world bounds from basemap + gauges */
const WB = (() => {
  let x0=1e9, y0=1e9, x1=-1e9, y1=-1e9;
  const eat = (lon, lat) => {
    const x = px_(lon), y = py_(lat);
    if (x<x0) x0=x; if (x>x1) x1=x; if (y<y0) y0=y; if (y>y1) y1=y;
  };
  for (const b of DATA.boroughs) for (const r of b.polys) for (const p of r) eat(p[0], p[1]);
  for (const g of DATA.gauges) eat(g.lon, g.lat);
  const mx = (x1-x0)*0.04, my = (y1-y0)*0.04;
  return {x0:x0-mx, y0:y0-my, x1:x1+mx, y1:y1+my};
})();

/* ---------- canvas map ---------- */
const cvm = document.getElementById('map');
const mx = cvm.getContext('2d');
let view = {k:1, tx:0, ty:0};          // screen = world*k + t
let W = 0, H = 0;

let fitK = 1;                          // world->pixel scale at "fit"; zoom clamps to it
function fitView(){
  const kx = W / (WB.x1 - WB.x0), ky = H / (WB.y1 - WB.y0);
  fitK = Math.min(kx, ky);
  view.k = fitK;
  view.tx = (W - (WB.x1-WB.x0)*view.k)/2 - WB.x0*view.k;
  view.ty = (H - (WB.y1-WB.y0)*view.k)/2 - WB.y0*view.k;
}
const sx = lon => px_(lon)*view.k + view.tx;
const sy = lat => py_(lat)*view.k + view.ty;

function sizeMap(reset){
  const r = cvm.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  W = r.width; H = r.height;
  cvm.width = W*dpr; cvm.height = H*dpr;
  mx.setTransform(dpr,0,0,dpr,0,0);
  if (reset || !view.k) fitView();
}

function boroughPath(ctx){
  ctx.beginPath();
  for (const b of DATA.boroughs)
    for (const r of b.polys){
      ctx.moveTo(sx(r[0][0]), sy(r[0][1]));
      for (let i=1;i<r.length;i++) ctx.lineTo(sx(r[i][0]), sy(r[i][1]));
      ctx.closePath();
    }
}

function drawMap(t, hi){
  mx.clearRect(0,0,W,H);
  mx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--water').trim();
  mx.fillRect(0,0,W,H);

  boroughPath(mx);
  mx.fillStyle = '#f7f8f7'; mx.fill('evenodd');

  boroughPath(mx);
  mx.strokeStyle = '#a9b7ba'; mx.lineWidth = 1; mx.stroke();

  const showLabels = document.getElementById('opt-labels').checked;
  for (const d of t){
    const frac = hi>0 ? d.total/hi : 0;
    const r = 6 + 20*Math.sqrt(frac);
    const X = sx(d.lon), Y = sy(d.lat);
    d._r = r; d._x = X; d._y = Y;
    mx.beginPath(); mx.arc(X,Y,r,0,6.2832);
    mx.fillStyle = rgb(ramp(frac)); mx.globalAlpha = 0.85; mx.fill(); mx.globalAlpha = 1;
    mx.lineWidth = (selected === d.id) ? 2.5 : 1.2;
    mx.strokeStyle = (selected === d.id) ? '#c2532b'
                   : (d.coverage < 0.99 || d.faults) ? '#c2532b' : '#12202b';
    mx.setLineDash((d.coverage < 0.99 || d.faults) ? [3,2.5] : []);
    mx.stroke();
    mx.setLineDash([]);
  }
  if (showLabels){
    mx.font = '600 11px ' + getComputedStyle(document.body).getPropertyValue('--mono');
    mx.textBaseline = 'middle';
    for (const d of t){
      const label = d.total.toFixed(2);
      const X = d._x + d._r + 4, Y = d._y;
      mx.lineWidth = 3; mx.strokeStyle = '#fff'; mx.strokeText(label, X, Y);
      mx.fillStyle = '#12202b'; mx.fillText(label, X, Y);
    }
  }
}

/* ---------- totals & render ---------- */
function totals(){
  const raw = document.getElementById('opt-raw').checked;
  return DATA.gauges.map(g => {
    let s = 0, ok = 0, n = 0, fault = 0, out = 0;
    for (let i = i0; i <= i1; i++){
      n++;
      const f = g.flags ? g.flags[i] : '0';
      if (f === '1') fault++; else if (f === '2') out++; else ok++;
      if (f === '0' || raw) s += g.series[i];
    }
    return {...g, total: s, coverage: n ? ok/n : 1, faults: fault, outage: out};
  });
}

let lastT = [], lastHi = 0;
function render(){
  const t = totals();
  const vals = t.map(d=>d.total);
  const hi = Math.max(...vals, 0.01);
  lastT = t; lastHi = hi;

  const match = EV.find(e => e.i0 === i0 && e.i1 === i1);
  document.getElementById('lg-hi').textContent = hi.toFixed(2);
  document.getElementById('brush-readout').textContent =
    (match ? labelFor(match) + ' · ' : '') + (i1-i0+1) + ' ' + DATA.step;
  if (document.activeElement !== d0el) d0el.value = DATA.periods[i0].slice(0,10);
  if (document.activeElement !== d1el) d1el.value = DATA.periods[i1].slice(0,10);

  drawMap(t, hi);

  const sorted = [...vals].sort((a,b)=>a-b);
  const med = sorted[Math.floor(sorted.length/2)];
  const odd = v => med > 0.5 && (v > 2.2*med || v < 0.45*med);

  const faulted = t.filter(d => d.faults > 0).length;
  const partial = t.filter(d => d.coverage < 0.99).length;
  const raw = document.getElementById('opt-raw').checked;
  const bits = [];
  if (faulted) bits.push(faulted + ' with sensor faults');
  if (partial) bits.push(partial + ' with gaps');
  document.getElementById('qa').textContent = bits.length
    ? (raw ? 'Showing unfiltered data — ' : 'Suspect data excluded — ') + bits.join(', ')
    : '';

  const rows = [...t].sort((a,b)=> b.total-a.total);
  document.getElementById('ranked').innerHTML = rows.map(d => {
    const frac = hi>0 ? d.total/hi : 0;
    let warn = '', sub = d.borough;
    if (d.faults) { warn = ` <span title="${d.faults} period(s) above the physical rate ceiling" style="color:#c2532b">&#9888;</span>`;
                    sub += ' · ' + d.faults + ' faulted'; }
    else if (d.outage) { warn = ` <span title="${d.outage} period(s) reading near zero while the network caught rain" style="color:#c2532b">&#9675;</span>`;
                    sub += ' · ' + Math.round(100*(1-d.coverage)) + '% offline'; }
    else if (odd(d.total)) { warn = ` <span title="${(d.total/med).toFixed(1)}x the network median" style="color:#b08a3a">&#9888;</span>`; }
    return `<tr class="${selected===d.id?'sel':''}" data-id="${d.id}">
      <td>${d.name}${warn}<br><span class="sub">${sub}</span></td>
      <td class="bar"><div class="barfill" style="width:${Math.max(2,frac*58)}px;background:${rgb(ramp(frac))}"></div></td>
      <td class="total">${d.total.toFixed(2)}</td></tr>`;
  }).join('');
  drawHyeto();
  drawChips();
}

document.getElementById('ranked').addEventListener('click', e => {
  const tr = e.target.closest('tr'); if (!tr) return;
  selected = (selected === tr.dataset.id) ? null : tr.dataset.id;
  render();
});

/* ---------- map interaction: drag pan, wheel + pinch zoom, tap to select ---------- */
let mdrag = null, pinch = null;
cvm.addEventListener('pointerdown', e => {
  cvm.setPointerCapture(e.pointerId);
  mdrag = {x:e.clientX, y:e.clientY, tx:view.tx, ty:view.ty, moved:0};
  cvm.classList.add('dragging');
});
cvm.addEventListener('pointermove', e => {
  if (!mdrag) return;
  const dx = e.clientX-mdrag.x, dy = e.clientY-mdrag.y;
  mdrag.moved = Math.max(mdrag.moved, Math.abs(dx)+Math.abs(dy));
  view.tx = mdrag.tx+dx; view.ty = mdrag.ty+dy;
  drawMap(lastT, lastHi);
});
cvm.addEventListener('pointerup', e => {
  cvm.classList.remove('dragging');
  if (mdrag && mdrag.moved < 5){
    const r = cvm.getBoundingClientRect(), px = e.clientX-r.left, py = e.clientY-r.top;
    let hit = null;
    for (const d of lastT)
      if (Math.hypot(px-d._x, py-d._y) <= d._r+5) hit = d.id;
    selected = (hit === selected) ? null : hit;
    render();
  }
  mdrag = null;
});
cvm.addEventListener('pointercancel', () => { mdrag = null; cvm.classList.remove('dragging'); });

function zoomAt(px, py, factor){
  const wx = (px-view.tx)/view.k, wy = (py-view.ty)/view.k;
  // view.k is an absolute degrees->pixels scale (~1300 at fit), NOT a relative factor,
  // so the bounds have to be expressed against fitK or the first click collapses the map
  view.k = Math.max(fitK*0.8, Math.min(fitK*40, view.k*factor));
  view.tx = px - wx*view.k; view.ty = py - wy*view.k;
  drawMap(lastT, lastHi);
}
cvm.addEventListener('wheel', e => {
  e.preventDefault();
  const r = cvm.getBoundingClientRect();
  zoomAt(e.clientX-r.left, e.clientY-r.top, e.deltaY < 0 ? 1.15 : 1/1.15);
}, {passive:false});
document.getElementById('zin').onclick  = () => zoomAt(W/2, H/2, 1.4);
document.getElementById('zout').onclick = () => zoomAt(W/2, H/2, 1/1.4);
document.getElementById('zfit').onclick = () => { fitView(); drawMap(lastT, lastHi); };

/* two-finger pinch */
cvm.addEventListener('touchstart', e => {
  if (e.touches.length === 2){
    mdrag = null;
    pinch = {d: Math.hypot(e.touches[0].clientX-e.touches[1].clientX,
                           e.touches[0].clientY-e.touches[1].clientY)};
  }
}, {passive:true});
cvm.addEventListener('touchmove', e => {
  if (pinch && e.touches.length === 2){
    e.preventDefault();
    const d = Math.hypot(e.touches[0].clientX-e.touches[1].clientX,
                         e.touches[0].clientY-e.touches[1].clientY);
    const r = cvm.getBoundingClientRect();
    zoomAt((e.touches[0].clientX+e.touches[1].clientX)/2 - r.left,
           (e.touches[0].clientY+e.touches[1].clientY)/2 - r.top, d/pinch.d);
    pinch.d = d;
  }
}, {passive:false});
cvm.addEventListener('touchend', () => { pinch = null; }, {passive:true});

/* ---------- hyetograph + brush (the period control) ---------- */
const d0el = document.getElementById('d0'), d1el = document.getElementById('d1');
const DAYS = DATA.periods.map(p => p.slice(0,10));
d0el.min = d1el.min = DAYS[0];
d0el.max = d1el.max = DAYS[N-1];

function applyDates(){
  if (!d0el.value || !d1el.value) return;
  let a = DAYS.findIndex(d => d >= d0el.value);
  let b = DAYS.length - 1 - [...DAYS].reverse().findIndex(d => d <= d1el.value);
  if (a < 0) a = 0;
  if (!isFinite(b) || b < 0 || b >= N) b = N - 1;
  i0 = Math.min(a, b); i1 = Math.max(a, b);
  render();
}
d0el.addEventListener('change', applyDates);
d1el.addEventListener('change', applyDates);

const cvh = document.getElementById('hyeto');
const hx = cvh.getContext('2d');
/* Bars show the WETTEST gauge in each period, not an average — a cell that soaks one
   sewershed is the signal we care about, and averaging flattens it. Flagged periods are
   skipped unless "Unfiltered" is on, so a faulty spike can't set the scale. */
function citywideSeries(){
  const raw = document.getElementById('opt-raw').checked;
  return DATA.periods.map((_, i) => {
    let m = 0;
    for (const g of DATA.gauges){
      const f = g.flags ? g.flags[i] : '0';
      if ((f === '0' || raw) && g.series[i] > m) m = g.series[i];
    }
    return m;
  });
}
let citywide = citywideSeries();
let peak = Math.max(...citywide, 0.01);

let HH = 60;                            // read from CSS so the media query can shrink it
function sizeHyeto(){
  const dpr = window.devicePixelRatio || 1;
  HH = cvh.clientHeight || 60;
  cvh.width = cvh.clientWidth*dpr; cvh.height = HH*dpr;
  hx.setTransform(dpr,0,0,dpr,0,0);
}
const xOf = i => (i/N)*cvh.clientWidth;
const iOf = p => Math.max(0, Math.min(N-1, Math.floor(p/cvh.clientWidth*N)));

function drawHyeto(){
  citywide = citywideSeries();
  peak = Math.max(...citywide, 0.01);
  const w = cvh.clientWidth, h = HH;
  hx.clearRect(0,0,w,h);
  const bw = Math.max(1, w/N - 0.5);
  citywide.forEach((v,i) => {
    const inWin = i>=i0 && i<=i1;
    const bh = Math.max(v>0?1:0, (v/peak)*(h-12));
    hx.fillStyle = inWin ? rgb(ramp(0.25+0.75*v/peak)) : '#d6dcdb';
    hx.fillRect(xOf(i), h-bh-10, bw, bh);
  });
  hx.strokeStyle = '#c9d2d3'; hx.lineWidth = 1;
  hx.beginPath(); hx.moveTo(0,h-9.5); hx.lineTo(w,h-9.5); hx.stroke();
  hx.fillStyle = 'rgba(194,83,43,0.08)';
  hx.fillRect(xOf(i0), 0, xOf(i1+1)-xOf(i0), h-9);
  hx.fillStyle = '#c2532b';
  hx.fillRect(xOf(i0), 0, 2, h-9);
  hx.fillRect(xOf(i1+1)-2, 0, 2, h-9);
}

let hdrag = null;
const hpx = e => e.clientX - cvh.getBoundingClientRect().left;
cvh.addEventListener('pointerdown', e => {
  cvh.setPointerCapture(e.pointerId);
  const x = hpx(e), a = xOf(i0), b = xOf(i1+1), grab = 12;
  if (Math.abs(x-a) < grab)      hdrag = {mode:'lo'};
  else if (Math.abs(x-b) < grab) hdrag = {mode:'hi'};
  else if (x > a && x < b)       hdrag = {mode:'pan', from:iOf(x), w0:i0, w1:i1};
  else { i0 = i1 = iOf(x); hdrag = {mode:'hi'}; render(); }
});
cvh.addEventListener('pointermove', e => {
  if (!hdrag) return;
  e.preventDefault();
  const i = iOf(hpx(e));
  if (hdrag.mode === 'lo') i0 = Math.min(i, i1);
  else if (hdrag.mode === 'hi') i1 = Math.max(i, i0);
  else {
    const shift = i-hdrag.from, len = hdrag.w1-hdrag.w0;
    i0 = Math.max(0, Math.min(N-1-len, hdrag.w0+shift)); i1 = i0+len;
  }
  render();
});
['pointerup','pointercancel'].forEach(ev => cvh.addEventListener(ev, () => { hdrag = null; }));

/* ---------- storm events ---------- */
const EV = DATA.events || [];
function labelFor(e){ return e.name || e.start; }
function drawChips(){
  const box = document.getElementById('events');
  if (!EV.length){ box.style.display = 'none'; return; }
  box.innerHTML = EV.map((e, k) => {
    const on = (i0 === e.i0 && i1 === e.i1) ? ' on' : '';
    return `<button class="chip${on}" data-k="${k}" title="${e.start} to ${e.end}">` +
           `<b>${labelFor(e)}</b><span class="d">${e.depth.toFixed(2)}"</span></button>`;
  }).join('');
}
document.getElementById('events').addEventListener('click', ev => {
  const c = ev.target.closest('.chip'); if (!c) return;
  const e = EV[+c.dataset.k];
  i0 = e.i0; i1 = e.i1;
  render();
  c.scrollIntoView({block:'nearest', inline:'center', behavior:'smooth'});
});

/* ---------- controls ---------- */
document.getElementById('btn-all').onclick = () => { i0 = 0; i1 = N-1; render(); };
document.getElementById('opt-labels').onchange  = () => render();
document.getElementById('opt-raw').onchange     = () => render();
document.getElementById('btn-csv').onclick = () => {
  const q = v => {
    const t = String(v ?? '');
    return /[",\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
  };
  const raw = document.getElementById('opt-raw').checked;
  const rows = [...lastT].sort((a,b) => b.total - a.total);
  const lines = [
    '# Cumulative rainfall, NYC DEP gauge network - independent project, not affiliated with NYC DEP',
    '# window: ' + DATA.periods[i0] + ' to ' + DATA.periods[i1] +
      ' (' + (i1 - i0 + 1) + ' ' + DATA.step + ')',
    '# quality filtering: ' + (raw ? 'OFF - suspect readings included' : 'ON - suspect readings excluded'),
    '# generated: ' + DATA.generated,
    ['gauge_id','gauge_name','borough','lat','lon','period_start','period_end',
     'total_in','coverage_pct','faulted_periods','offline_periods'].join(',')
  ];
  for (const d of rows){
    lines.push([d.id, d.name, d.borough, d.lat, d.lon,
                DATA.periods[i0], DATA.periods[i1], d.total.toFixed(3),
                (100 * d.coverage).toFixed(1), d.faults, d.outage].map(q).join(','));
  }
  const name = 'rainfall_' + DATA.periods[i0].slice(0,10) + '_' +
               DATA.periods[i1].slice(0,10) + (raw ? '_unfiltered' : '') + '.csv';
  const blob = new Blob([lines.join('\r\n') + '\r\n'], {type:'text/csv;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = name; a.style.display = 'none';
  document.body.appendChild(a);          // Firefox needs the anchor in the document
  a.click();
  setTimeout(() => { document.body.removeChild(a); URL.revokeObjectURL(url); }, 0);
};

/* ---------- mobile panes ---------- */
document.getElementById('tabs').addEventListener('click', e => {
  const b = e.target.closest('.tab'); if (!b) return;
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x === b));
  document.body.className = 'pane-' + b.dataset.pane;
  if (b.dataset.pane === 'map'){ sizeMap(false); }   // canvas had no size while hidden
  render();
});

window.addEventListener('resize', () => { sizeMap(false); sizeHyeto(); render(); });
document.body.className = 'pane-map';
sizeMap(true); sizeHyeto(); render();
</script>
</body>
</html>
"""


def write_html(out_path: Path, payload: dict, title: str):
    html = (HTML_TEMPLATE
            .replace("__TITLE__", title)
            .replace("__PAYLOAD__", json.dumps(payload, separators=(",", ":"))))
    out_path.write_text(html, encoding="utf-8")


def find_events(binned: pd.DataFrame, flags: pd.DataFrame, periods: list,
                names_path: Path | None, wet: float, top: int) -> list:
    """Group consecutive wet periods into storm events and attach names where known.

    Detection is mechanical — a run of periods where the network median exceeds
    `wet`. Names come from an editable CSV (name,start,end); anything not listed
    keeps its date, so an unnamed storm is still selectable.
    """
    valid = binned.where(flags == 0)
    med = valid.median(axis=1).fillna(0.0)
    is_wet = med > wet
    runs, start = [], None
    for i, w in enumerate(is_wet.to_numpy()):
        if w and start is None:
            start = i
        elif not w and start is not None:
            runs.append((start, i - 1)); start = None
    if start is not None:
        runs.append((start, len(is_wet) - 1))

    named = []
    path = names_path or (Path(__file__).resolve().parent / "events.csv")
    if path.exists():
        try:
            nm = pd.read_csv(path)
            named = [(str(r["name"]), str(r["start"])[:10], str(r["end"])[:10])
                     for _, r in nm.iterrows()]
        except Exception as e:
            print(f"  ! could not read {path.name}: {e}", file=sys.stderr)

    events = []
    for a, b in runs:
        # detection uses the median (robust to one bad gauge); the number shown is the
        # wettest single gauge's total, so it matches what the hyetograph bars display
        depth = float(valid.iloc[a:b + 1].sum(min_count=1).max())
        sd, ed = periods[a][:10], periods[b][:10]
        label = None
        for name, ns, ne in named:            # overlap, not exact match
            if ns <= ed and sd <= ne:
                label = name; break
        events.append({"i0": a, "i1": b, "depth": round(depth, 2),
                       "name": label, "start": sd, "end": ed})
    events.sort(key=lambda e: -e["depth"])
    keep = events[:top]
    keep += [e for e in events if e["name"] and e not in keep]
    keep.sort(key=lambda e: e["i0"])
    if keep:
        n_named = sum(1 for e in keep if e["name"])
        print(f"  {len(keep)} events marked ({n_named} named), "
              f"largest {keep and max(keep, key=lambda e: e['depth'])['depth']} in")
    return keep


def load_basemap(explicit: Path | None) -> list:
    path = explicit or (Path(__file__).resolve().parent / "boroughs.json")
    if not path.exists():
        print(f"  ! basemap {path.name} not found — drawing gauges without borough outlines",
              file=sys.stderr)
        return []
    return json.loads(path.read_text())["boroughs"]


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", required=True, nargs="+",
                   help="readings file(s), a directory, or a glob. With several files the "
                        "FILENAME is taken as the gauge id.")
    p.add_argument("--out", type=Path, default=Path("rainfall_map.html"))
    p.add_argument("--gauges", type=Path, help="CSV of gauge sites: id,name,borough,lat,lon")
    p.add_argument("--basemap", type=Path, help="borough outline JSON (default: boroughs.json)")
    p.add_argument("--mode", choices=["incremental", "running"], default="incremental")
    p.add_argument("--freq", default="D", help="aggregation bucket: D (default), h, W, MS")
    p.add_argument("--since", help="drop readings before this date (trims the embedded record)")
    p.add_argument("--until", help="drop readings after this date")
    p.add_argument("--start", help="initial window start YYYY-MM-DD (adjustable in browser)")
    p.add_argument("--end", help="initial window end YYYY-MM-DD (adjustable in browser)")
    p.add_argument("--gauge-col"); p.add_argument("--time-col"); p.add_argument("--value-col")
    p.add_argument("--events", type=Path,
                   help="CSV of named storms (name,start,end); default: events.csv beside this script")
    p.add_argument("--event-wet", type=float, default=0.10,
                   help="network-median depth per period that counts as wet (default: 0.10)")
    p.add_argument("--event-top", type=int, default=14,
                   help="how many of the largest events to mark (default: 14)")
    qa = p.add_argument_group("data quality")
    qa.add_argument("--qa", choices=["on", "off"], default="on",
                    help="flag sensor faults and outages (default: on)")
    qa.add_argument("--max-rate", type=float, default=8.0,
                    help="hard in/hr ceiling — always a fault (default: 8.0; Ida peaked near 3.2)")
    qa.add_argument("--suspect-rate", type=float, default=2.5,
                    help="in/hr above which a reading needs corroboration to be kept (default: 2.5)")
    qa.add_argument("--corroboration", type=float, default=0.2,
                    help="share of the reading a peer gauge must also catch (default: 0.2)")
    qa.add_argument("--offline-window", type=int, default=30,
                    help="days over which to compare a gauge to the network (default: 30)")
    qa.add_argument("--offline-ratio", type=float, default=0.15,
                    help="below this share of the network median, a gauge reads as offline")
    qa.add_argument("--offline-floor", type=float, default=1.0,
                    help="inches the network must catch before an outage can be called")
    args = p.parse_args()

    sites = (pd.read_csv(args.gauges) if args.gauges
             else pd.DataFrame(GAUGE_SITES, columns=["id", "name", "borough", "lat", "lon"]))
    for col in ("id", "name", "borough", "lat", "lon"):
        if col not in sites.columns:
            sys.exit(f"error: --gauges file needs an '{col}' column")

    readings = load_many(args.input, args.gauge_col, args.time_col, args.value_col)
    if args.since:
        readings = readings[readings["ts"] >= pd.Timestamp(args.since)]
    if args.until:
        readings = readings[readings["ts"] <= pd.Timestamp(args.until) + pd.Timedelta(days=1)]
    readings = resolve_gauges(readings, sites)
    if readings.empty:
        sys.exit("error: no readings matched a known gauge site")
    readings = to_increments(readings, args.mode)
    readings = mark_spikes(readings, args.max_rate, args.suspect_rate, args.corroboration)
    periods, binned, spiked = build_series(readings, args.freq)

    if args.qa == "off":
        flags = pd.DataFrame(0, index=binned.index, columns=binned.columns)
    else:
        per_day = {"D": 1, "h": 24, "W": 1/7, "MS": 1/30}.get(args.freq, 1)
        window = max(3, int(round(args.offline_window * per_day)))
        offline = mark_offline(binned, window, args.offline_ratio, args.offline_floor)
        flags = (spiked.astype(int) * 1).where(spiked, offline.astype(int) * 2)
        bad = int((flags > 0).sum().sum())
        if bad:
            worst = (flags > 0).sum().sort_values(ascending=False)
            top = ", ".join(f"{g} {int(n)}" for g, n in worst[worst > 0].head(4).items())
            print(f"  ! QA flagged {bad} gauge-periods (worst: {top})", file=sys.stderr)

    lookup = sites.set_index(sites["id"].astype(str)).to_dict("index")
    gauges = []
    for gid in binned.columns:
        m = lookup[str(gid)]
        gauges.append({"id": str(gid), "name": str(m["name"]), "borough": str(m["borough"]),
                       "lat": float(m["lat"]), "lon": float(m["lon"]),
                       "series": [round(float(x), 4) for x in binned[gid].to_numpy()],
                       "flags": "".join(str(int(f)) for f in flags[gid].to_numpy())})
    gauges.sort(key=lambda g: g["name"])

    lo, hi = 0, len(periods) - 1
    days = [p[:10] for p in periods]
    if args.start:
        lo = max(0, min(hi, int(np.searchsorted(days, args.start[:10], "left"))))
    if args.end:
        hi = max(lo, min(hi, int(np.searchsorted(days, args.end[:10], "right")) - 1))

    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "step": {"D": "days", "h": "hours", "W": "weeks", "MS": "months"}.get(args.freq, "steps"),
        "periods": periods,
        "initial": [lo, hi],
        "gauges": gauges,
        "events": find_events(binned, flags, periods, args.events,
                              args.event_wet, args.event_top),
        "boroughs": load_basemap(args.basemap),
    }
    write_html(args.out, payload,
               "Cumulative rainfall — NYC DEP gauge network (independent project)")
    print(f"  {len(gauges)} gauges · {len(periods)} {payload['step']} "
          f"({periods[0]} → {periods[-1]})")
    print(f"  wrote {args.out.resolve()} ({args.out.stat().st_size/1024:.0f} KB, no external requests)")


if __name__ == "__main__":
    main()
