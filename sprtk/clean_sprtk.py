#!/usr/bin/env python3
"""Clean NYSDEC SPRTK sewage discharge reports (SPRTK_Data.xlsx, 2021+).

Normalizes all monthly/cumulative tabs (4 schema eras, 3 date formats) into
one canonical table, drops blank padding rows, deduplicates cross-tab
repeats, and exports a statewide file plus an NYC subset.

Usage:
    python clean_sprtk.py SPRTK_Data.xlsx [output_dir]

Source: https://dec.ny.gov/environmental-protection/water/water-quality/sewage-pollution-right-to-know
Known limitations of the source data (as of the July 2026 download):
  - Coverage ends 2025-11-30 despite "through current" labeling; no 2026 tabs.
  - "December 2025" tab actually contains November 2025 records.
  - Nov/Dec 2021 tabs are cumulative dumps padded with ~6k blank rows each.
  - DEC does not QC submissions: durations are free text, volumes mix units,
    reason field contains comma-duplication artifacts.
"""
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# canonical column -> aliases seen across the four schema eras
CMAP = {
    "facility":     ["POTW/POSS Name", "Facility Name", "facilityName", "facility name"],
    "facility_id":  ["POTW/POSS ID", "Facility ID", "facilityId", "sprtk id"],
    "start":        ["Discharge Start Date", "Start Date", "sprtkStartDischargeDatetime", "start time"],
    "end":          ["Discharge End Date", "End Date", "sprtkEndDischargeDatetime"],
    "duration":     ["Duration", "dischargeDuration", "duration"],
    "waterbody":    ["Receiving Water Body", "receivingWaterBody", "affected water body"],
    "quantity":     ["Quantity/Volume", "quantity"],
    "treated":      ["Treated State", "treatedState", "treated state"],
    "reason":       ["Reason", "dischargeReason", "reasons for discharge"],
    "county":       ["County", "countyName", "county"],
    "city":         ["City", "city"],
    "status":       ["Notification Status", "notificationStatus"],
    "incident_id":  ["Incident ID", "incidentId"],
    "notif_id":     ["Notification ID", "notificationId"],
    "lat":          ["Latitude", "latitude"],
    "lon":          ["Longitude", "longitude"],
}

NYC_COUNTIES = {"bronx", "kings", "queens", "new york", "richmond",
                "brooklyn", "manhattan", "staten island"}

EXPORT_COLS = ["start_dt", "end", "duration", "duration_hours", "facility",
               "facility_id", "waterbody", "quantity", "treated", "reason",
               "county", "city", "status", "incident_id", "lat", "lon", "sheet"]


def normalize_sheet(df: pd.DataFrame, sheet: str) -> pd.DataFrame:
    out = pd.DataFrame()
    for canon, aliases in CMAP.items():
        col = next((a for a in aliases if a in df.columns), None)
        out[canon] = df[col] if col else None
    out["sheet"] = sheet
    return out


def parse_dates(s: pd.Series) -> pd.Series:
    """Try ISO, then US datetime, US datetime w/o seconds, US date, then dateutil."""
    s = s.astype(str).str.strip()
    d = pd.to_datetime(s, errors="coerce", format="ISO8601")
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        m = d.isna()
        if not m.any():
            break
        d[m] = pd.to_datetime(s[m], errors="coerce", format=fmt)
    m = d.isna()
    if m.any():
        d[m] = pd.to_datetime(s[m], errors="coerce")
    return d


def duration_hours(v) -> float:
    """Parse free-text duration ('24 Hours', '15 Minutes', '2 Days') to hours."""
    s = str(v).strip().lower()
    if not s or s in ("nan", "none", "unknown", "tbd"):
        return np.nan
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(hour|hr|minute|min|day)", s)
    if m:
        n, unit = float(m.group(1)), m.group(2)
        if unit.startswith("min"):
            return n / 60
        if unit == "day":
            return n * 24
        return n
    m = re.match(r"^(\d+(?:\.\d+)?)$", s)
    return float(m.group(1)) if m else np.nan


def main(xlsx_path: str, out_dir: str = ".") -> None:
    xl = pd.ExcelFile(xlsx_path)
    df = pd.concat(
        [normalize_sheet(xl.parse(s), s) for s in xl.sheet_names],
        ignore_index=True,
    )
    raw = len(df)

    df["start_dt"] = parse_dates(df["start"])
    df = df[df["start_dt"].notna()].copy()  # drops blank padding rows

    # cross-tab dedup: cumulative tabs repeat monthly-tab records
    df["dupkey"] = (
        df["facility"].astype(str).str.strip().str.lower() + "|"
        + df["start_dt"].astype(str) + "|"
        + df["waterbody"].astype(str).str.strip().str.lower() + "|"
        + df["duration"].astype(str).str.strip().str.lower()
    )
    df = df.sort_values("start_dt").drop_duplicates("dupkey", keep="last")

    df["duration_hours"] = df["duration"].apply(duration_hours)

    county = df["county"].astype(str).str.strip().str.lower()
    is_nyc = county.isin(NYC_COUNTIES) | df["facility"].astype(str).str.contains(
        "NYCDEP", case=False, na=False
    )

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    df[EXPORT_COLS].to_csv(out / "sprtk_statewide_deduped.csv", index=False)
    df[is_nyc][EXPORT_COLS].to_csv(out / "sprtk_nyc_cleaned.csv", index=False)
    print(f"{raw} raw rows -> {len(df)} deduped ({is_nyc.sum()} NYC)")
    print(f"coverage: {df['start_dt'].min()} -> {df['start_dt'].max()}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ".")
