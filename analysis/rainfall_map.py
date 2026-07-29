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
    "26th ward": "26W", "26w": "26W", "26 ward": "26W", "twenty sixth ward": "26W",
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
TIME_COL_HINTS = ["datetime", "date_time", "timestamp", "reading_time", "obs_time",
                  "observed_at", "date", "time"]
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
    sys.exit(f"error: could not auto-detect the {kind} column. "
             f"Pass --{kind}-col explicitly. Available: {list(columns)}")


def load_readings(path: Path, gauge_col=None, time_col=None, value_col=None) -> pd.DataFrame:
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

    g = _pick_column(df.columns, GAUGE_COL_HINTS, gauge_col, "gauge")
    t = _pick_column(df.columns, TIME_COL_HINTS, time_col, "time")
    v = _pick_column(df.columns, VALUE_COL_HINTS, value_col, "value")

    out = df[[g, t, v]].copy()
    out.columns = ["gauge_raw", "ts", "value"]
    out["ts"] = pd.to_datetime(out["ts"], errors="coerce", format="mixed")
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    out = out.dropna(subset=["ts", "value"])
    if out.empty:
        sys.exit("error: no rows survived timestamp/value parsing — check --time-col and --value-col")
    return out


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


def build_series(readings: pd.DataFrame, freq: str):
    binned = (readings.set_index("ts").groupby("gauge")["inc"]
              .resample(freq).sum().unstack(0).fillna(0.0).sort_index())
    fmt = "%Y-%m-%d" if freq.lower().startswith("d") else "%Y-%m-%d %H:00"
    labels = [d.strftime(fmt) for d in binned.index]
    series = {c: [round(float(x), 4) for x in binned[c].to_numpy()] for c in binned.columns}
    return labels, series


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
  .eyebrow{font:500 10px/1 var(--mono);letter-spacing:.15em;text-transform:uppercase;
           color:#5c6f78;display:block;margin-bottom:5px}
  .readout{margin-left:auto;display:flex;gap:22px;flex-wrap:wrap}
  .readout div{font:400 10.5px/1.3 var(--mono);color:#5c6f78}
  .readout b{display:block;font:600 18px/1.15 var(--mono);color:var(--ink)}

  main{flex:1;display:flex;min-height:0}
  .mapwrap{flex:1;min-width:0;position:relative;background:var(--water)}
  #map{width:100%;height:100%;display:block;touch-action:none;cursor:grab}
  #map.dragging{cursor:grabbing}
  .legend{position:absolute;left:12px;bottom:12px;background:rgba(255,255,255,.94);
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
  .brushlabel strong{font-weight:600;color:var(--ink);font-size:11.5px;white-space:nowrap}
  #hyeto{width:100%;height:60px;display:block;cursor:col-resize;touch-action:none}
  .opts{display:flex;gap:14px;align-items:center;flex-wrap:wrap;font-size:12.5px;color:#40545e}
  .opts label{display:flex;gap:6px;align-items:center;cursor:pointer;user-select:none}
  button{font:500 12px/1 var(--sans);color:var(--ink);background:var(--panel);
         border:1px solid var(--rule);border-radius:3px;padding:7px 10px;cursor:pointer}
  button:hover{border-color:var(--ink)}
  :focus-visible{outline:2px solid var(--alert);outline-offset:2px}

  @media (max-width:860px){
    main{flex-direction:column}
    .mapwrap{height:44vh;flex:none}
    aside{width:100%;border-left:0;border-top:1px solid var(--rule);max-height:32vh}
    .readout{margin-left:0;gap:18px}
    .readout b{font-size:15px}
    header{padding:10px 14px 8px}
    .controls{padding:8px 14px 12px}
  }
</style>
</head>
<body>
<header>
  <div>
    <span class="eyebrow">NYC DEP rain gauge network &middot; water resource recovery facilities</span>
    <h1>Cumulative rainfall by gauge</h1>
  </div>
  <div class="readout">
    <div>Window<b id="r-window">&mdash;</b></div>
    <div>Network mean<b id="r-mean">&mdash;</b></div>
    <div>Wettest<b id="r-max">&mdash;</b></div>
    <div>Spread<b id="r-spread">&mdash;</b></div>
  </div>
</header>

<main>
  <div class="mapwrap">
    <canvas id="map"></canvas>
    <div class="mapbtns">
      <button id="zin" title="Zoom in">+</button>
      <button id="zout" title="Zoom out">&minus;</button>
      <button id="zfit" title="Reset view" style="font-size:11px">fit</button>
    </div>
    <div class="legend">
      <div>Depth over window (in)</div>
      <div class="ramp" id="lg-ramp"></div>
      <div class="rampends"><span>0</span><span id="lg-hi">&mdash;</span></div>
      <div style="margin-top:5px">Circle area &prop; depth</div>
    </div>
  </div>
  <aside>
    <h2>Gauge totals (in)</h2>
    <table><tbody id="ranked"></tbody></table>
    <p class="note">Totals are the sum of gauge increments inside the selected window.
    Drag across the hyetograph to change the period, or drag the shaded band to slide it.</p>
  </aside>
</main>

<div class="controls">
  <div class="brushwrap">
    <div class="brushlabel">
      <span>Citywide daily mean depth &mdash; drag to select</span>
      <strong id="brush-readout">&mdash;</strong>
    </div>
    <canvas id="hyeto"></canvas>
  </div>
  <div class="opts">
    <label><input type="checkbox" id="opt-surface"> Interpolated surface</label>
    <label><input type="checkbox" id="opt-labels" checked> Labels</label>
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

function fitView(){
  const kx = W / (WB.x1 - WB.x0), ky = H / (WB.y1 - WB.y0);
  view.k = Math.min(kx, ky);
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

let surfaceCv = null, surfaceBox = null;
function buildSurface(t, hi){
  surfaceCv = null;
  if (!document.getElementById('opt-surface').checked) return;
  const pad = 0.05;
  const lats = t.map(d=>d.lat), lons = t.map(d=>d.lon);
  surfaceBox = {lon0:Math.min(...lons)-pad, lon1:Math.max(...lons)+pad,
                lat0:Math.min(...lats)-pad, lat1:Math.max(...lats)+pad};
  const S = 220;
  const cv = document.createElement('canvas'); cv.width = S; cv.height = S;
  const c = cv.getContext('2d'); const img = c.createImageData(S,S);
  for (let y=0;y<S;y++){
    const lat = surfaceBox.lat1 - (y+0.5)/S*(surfaceBox.lat1-surfaceBox.lat0);
    for (let x=0;x<S;x++){
      const lon = surfaceBox.lon0 + (x+0.5)/S*(surfaceBox.lon1-surfaceBox.lon0);
      let num=0, den=0;
      for (const d of t){
        const dx=(lon-d.lon)*KX, dy=lat-d.lat, d2=dx*dx+dy*dy;
        if (d2 < 1e-10){ num=d.total; den=1; break; }
        const w = 1/(d2*d2);
        num += w*d.total; den += w;
      }
      const col = ramp(hi>0 ? (den?num/den:0)/hi : 0);
      const p=(y*S+x)*4;
      img.data[p]=col[0]; img.data[p+1]=col[1]; img.data[p+2]=col[2]; img.data[p+3]=205;
    }
  }
  c.putImageData(img,0,0);
  surfaceCv = cv;
}

function drawMap(t, hi){
  mx.clearRect(0,0,W,H);
  mx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--water').trim();
  mx.fillRect(0,0,W,H);

  boroughPath(mx);
  mx.fillStyle = '#f7f8f7'; mx.fill('evenodd');

  if (surfaceCv){
    mx.save(); boroughPath(mx); mx.clip('evenodd');
    const x = sx(surfaceBox.lon0), y = sy(surfaceBox.lat1);
    mx.imageSmoothingEnabled = true;
    mx.drawImage(surfaceCv, x, y, sx(surfaceBox.lon1)-x, sy(surfaceBox.lat0)-y);
    mx.restore();
  }

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
    mx.strokeStyle = (selected === d.id) ? '#c2532b' : '#12202b';
    mx.stroke();
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
  return DATA.gauges.map(g => {
    let s = 0;
    for (let i = i0; i <= i1; i++) s += g.series[i];
    return {...g, total: s};
  });
}

let lastT = [], lastHi = 0;
function render(rebuildSurface){
  const t = totals();
  const vals = t.map(d=>d.total);
  const hi = Math.max(...vals, 0.01);
  const mean = vals.reduce((a,b)=>a+b,0)/vals.length;
  const wettest = t.reduce((a,b)=> b.total>a.total ? b : a);
  const driest  = t.reduce((a,b)=> b.total<a.total ? b : a);
  lastT = t; lastHi = hi;

  document.getElementById('r-window').textContent  = DATA.periods[i0] + ' → ' + DATA.periods[i1];
  document.getElementById('r-mean').textContent    = mean.toFixed(2) + '"';
  document.getElementById('r-max').textContent     = wettest.total.toFixed(2) + '" ' + wettest.id;
  document.getElementById('r-spread').textContent  = (wettest.total-driest.total).toFixed(2) + '"';
  document.getElementById('lg-hi').textContent     = hi.toFixed(2);
  document.getElementById('brush-readout').textContent =
    DATA.periods[i0] + ' – ' + DATA.periods[i1] + ' (' + (i1-i0+1) + ' ' + DATA.step + ')';

  if (rebuildSurface !== false) buildSurface(t, hi);
  drawMap(t, hi);

  const rows = [...t].sort((a,b)=> b.total-a.total);
  document.getElementById('ranked').innerHTML = rows.map(d => {
    const frac = hi>0 ? d.total/hi : 0;
    return `<tr class="${selected===d.id?'sel':''}" data-id="${d.id}">
      <td>${d.name}<br><span class="sub">${d.borough}</span></td>
      <td class="bar"><div class="barfill" style="width:${Math.max(2,frac*58)}px;background:${rgb(ramp(frac))}"></div></td>
      <td class="total">${d.total.toFixed(2)}</td></tr>`;
  }).join('');
  drawHyeto();
}

document.getElementById('ranked').addEventListener('click', e => {
  const tr = e.target.closest('tr'); if (!tr) return;
  selected = (selected === tr.dataset.id) ? null : tr.dataset.id;
  render(false);
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
    render(false);
  }
  mdrag = null;
});
cvm.addEventListener('pointercancel', () => { mdrag = null; cvm.classList.remove('dragging'); });

function zoomAt(px, py, factor){
  const wx = (px-view.tx)/view.k, wy = (py-view.ty)/view.k;
  view.k = Math.max(0.25, Math.min(60, view.k*factor)) ;
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
const cvh = document.getElementById('hyeto');
const hx = cvh.getContext('2d');
const citywide = DATA.periods.map((_, i) =>
  DATA.gauges.reduce((s,g)=> s+g.series[i], 0)/DATA.gauges.length);
const peak = Math.max(...citywide, 0.01);

function sizeHyeto(){
  const dpr = window.devicePixelRatio || 1;
  cvh.width = cvh.clientWidth*dpr; cvh.height = 60*dpr;
  hx.setTransform(dpr,0,0,dpr,0,0);
}
const xOf = i => (i/N)*cvh.clientWidth;
const iOf = p => Math.max(0, Math.min(N-1, Math.floor(p/cvh.clientWidth*N)));

function drawHyeto(){
  const w = cvh.clientWidth, h = 60;
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

/* ---------- controls ---------- */
document.getElementById('btn-all').onclick = () => { i0 = 0; i1 = N-1; render(); };
document.getElementById('opt-surface').onchange = () => render();
document.getElementById('opt-labels').onchange  = () => render(false);
document.getElementById('btn-csv').onclick = () => {
  const csv = ['gauge_id,gauge_name,borough,lat,lon,period_start,period_end,total_in']
    .concat(lastT.map(d => [d.id, `"${d.name}"`, d.borough, d.lat, d.lon,
                            DATA.periods[i0], DATA.periods[i1], d.total.toFixed(3)].join(',')))
    .join('\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], {type:'text/csv'}));
  a.download = ('rainfall_' + DATA.periods[i0] + '_' + DATA.periods[i1] + '.csv').replace(/[: ]/g,'');
  a.click();
};

window.addEventListener('resize', () => { sizeMap(false); sizeHyeto(); render(false); });
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
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--out", type=Path, default=Path("rainfall_map.html"))
    p.add_argument("--gauges", type=Path, help="CSV of gauge sites: id,name,borough,lat,lon")
    p.add_argument("--basemap", type=Path, help="borough outline JSON (default: boroughs.json)")
    p.add_argument("--mode", choices=["incremental", "running"], default="incremental")
    p.add_argument("--freq", default="D", help="aggregation bucket: D (default), h, W, MS")
    p.add_argument("--start", help="initial window start YYYY-MM-DD (adjustable in browser)")
    p.add_argument("--end", help="initial window end YYYY-MM-DD (adjustable in browser)")
    p.add_argument("--gauge-col"); p.add_argument("--time-col"); p.add_argument("--value-col")
    args = p.parse_args()

    sites = (pd.read_csv(args.gauges) if args.gauges
             else pd.DataFrame(GAUGE_SITES, columns=["id", "name", "borough", "lat", "lon"]))
    for col in ("id", "name", "borough", "lat", "lon"):
        if col not in sites.columns:
            sys.exit(f"error: --gauges file needs an '{col}' column")

    readings = load_readings(args.input, args.gauge_col, args.time_col, args.value_col)
    readings = resolve_gauges(readings, sites)
    if readings.empty:
        sys.exit("error: no readings matched a known gauge site")
    readings = to_increments(readings, args.mode)
    periods, series = build_series(readings, args.freq)

    lookup = sites.set_index(sites["id"].astype(str)).to_dict("index")
    gauges = []
    for gid, vals in series.items():
        m = lookup[str(gid)]
        gauges.append({"id": str(gid), "name": str(m["name"]), "borough": str(m["borough"]),
                       "lat": float(m["lat"]), "lon": float(m["lon"]), "series": vals})
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
        "boroughs": load_basemap(args.basemap),
    }
    write_html(args.out, payload, "NYC DEP rain gauge network — cumulative rainfall")
    print(f"  {len(gauges)} gauges · {len(periods)} {payload['step']} "
          f"({periods[0]} → {periods[-1]})")
    print(f"  wrote {args.out.resolve()} ({args.out.stat().st_size/1024:.0f} KB, no external requests)")


if __name__ == "__main__":
    main()
