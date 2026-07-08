import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
import requests

# DEP Waterbody Advisory logger, v2 â€” matched to the real API.
# The server publishes only (a) rain gauges and (b) the advisory model
# config per waterbody. Advisories are computed client-side by the
# dashboard, so we log the inputs and a provisional trigger status here;
# exact duration math is reproduced in analysis once decoded from the
# site's JavaScript (archived below into site_js/).

BASE = "https://nycwaterbodyadvisory.azurewebsites.net/"
ENDPOINTS = {
    "waterbodies": BASE + "api/waterbodies",
    "sensors": BASE + "api/sensors",
    "advisory": BASE + "api/advisory",
}
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"
HDRS = {"User-Agent": "Mozilla/5.0 (personal research logger)",
        "Accept": "application/json, text/plain, */*"}

WB_COLS = ["poll_time_utc", "waterbody", "wbid", "sensor_id", "rain_24h_in",
           "wq_threshold_in", "wq_coeff_a", "wq_coeff_b",
           "cso_threshold_in", "provisional_on_advisory"]
SN_COLS = ["poll_time_utc", "sensor_id", "sensor_name", "rain_24h_in", "active"]
ADV_COLS = ["poll_time_utc", "advisory_type", "waterbody", "wb_api_id",
            "wb_class", "rain_gauge", "occurred_on", "duration_hr", "volume"]


def fetch_json(url):
    r = requests.get(url, headers=HDRS, timeout=30)
    r.raise_for_status()
    return r.json()


def advisory_type(wb, short):
    for at in wb.get("advisoryTypes") or []:
        if at.get("shortName") == short:
            return at
    return {}


def append(path, cols, rows):
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerows(rows)


def snapshot_model_config(waterbodies, t):
    """Keep one copy of the model rulebook; save a new dated copy only
    when DEP changes it (ignoring volatile fields like createdOn)."""
    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in sorted(o.items())
                    if k not in ("createdOn", "$type", "activeSensor")}
        if isinstance(o, list):
            return [strip(x) for x in o]
        return o
    canon = json.dumps(strip(waterbodies), sort_keys=True)
    cfg_dir = DATA / "model_config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    latest = cfg_dir / "latest.json"
    if latest.exists() and latest.read_text() == canon:
        return
    latest.write_text(canon)
    (cfg_dir / f"config_{t:%Y%m%dT%H%M%SZ}.json").write_text(
        json.dumps(waterbodies, indent=1))
    print("Model config changed (or first run) â€” snapshot saved.")


def archive_site_js():
    """One-time: save the dashboard's JS bundles into the repo so the
    exact advisory-duration formula can be decoded from them."""
    import re
    from urllib.parse import urljoin
    out = ROOT / "site_js"
    if out.exists() and any(out.iterdir()):
        return
    out.mkdir(exist_ok=True)
    try:
        html = requests.get(BASE, headers=HDRS, timeout=30).text
        for src in re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.I):
            url = urljoin(BASE, src)
            name = src.strip("/").replace("/", "__") or "inline.js"
            try:
                js = requests.get(url, headers=HDRS, timeout=30).text
                (out / name).write_text(js)
                print(f"Archived JS: {name} ({len(js)} chars)")
            except requests.RequestException as e:
                print(f"[warn] JS fetch {url}: {e}")
    except requests.RequestException as e:
        print(f"[warn] could not archive site JS: {e}")


def main():
    t = datetime.now(timezone.utc)
    try:
        waterbodies = fetch_json(ENDPOINTS["waterbodies"])
        sensors = fetch_json(ENDPOINTS["sensors"])
    except (requests.RequestException, ValueError) as e:
        sys.exit(f"API fetch failed: {e}")

    sn_rows = [{
        "poll_time_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sensor_id": s.get("id"),
        "sensor_name": s.get("name"),
        "rain_24h_in": round(float(s.get("accumulatedLast24Hours") or 0), 4),
        "active": s.get("active"),
    } for s in sensors]

    wb_rows = []
    for wb in waterbodies:
        wq = advisory_type(wb, "WQ")
        cso = advisory_type(wb, "CSO")
        sensor = wb.get("activeSensor") or {}
        rain = round(float(sensor.get("accumulatedLast24Hours") or 0), 4)
        wq_thr = wq.get("rainfallThreshold")
        coeffs = wq.get("coefficients") or [None, None]
        wb_rows.append({
            "poll_time_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "waterbody": wb.get("name"),
            "wbid": wb.get("wbid"),
            "sensor_id": sensor.get("id"),
            "rain_24h_in": rain,
            "wq_threshold_in": wq_thr,
            "wq_coeff_a": coeffs[0] if len(coeffs) > 0 else None,
            "wq_coeff_b": coeffs[1] if len(coeffs) > 1 else None,
            "cso_threshold_in": cso.get("rainfallThreshold"),
            # Provisional: 24-h rain at the gauge meets/exceeds the WQ
            # trigger. The exact DEP logic uses storm-event depth with a
            # 6-h gap; this proxy is refined in analysis.
            "provisional_on_advisory": (wq_thr is not None and rain >= wq_thr),
        })

    # DEP's own computed advisories (the authoritative record).
    # Only waterbodies currently ON advisory appear in these responses;
    # absence from the list means no advisory at this poll.
    adv_rows = []
    for adv_type in ("WQ", "CSO"):
        try:
            advisories = requests.get(
                ENDPOINTS["advisory"], params={"advisoryType": adv_type},
                headers=HDRS, timeout=30).json()
        except (requests.RequestException, ValueError) as e:
            print(f"[warn] advisory {adv_type} fetch failed: {e}")
            continue
        for a in advisories:
            wb = a.get("waterbody") or {}
            adv_rows.append({
                "poll_time_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "advisory_type": adv_type,
                "waterbody": wb.get("name"),
                "wb_api_id": wb.get("id"),
                "wb_class": wb.get("class"),
                "rain_gauge": wb.get("rainGauge"),
                "occurred_on": a.get("occurredOn"),
                "duration_hr": a.get("duration"),
                "volume": a.get("volume"),
            })
    if adv_rows:
        append(DATA / f"advisory_log_{t:%Y-%m}.csv", ADV_COLS, adv_rows)

    append(DATA / f"waterbody_log_{t:%Y-%m}.csv", WB_COLS, wb_rows)
    append(DATA / f"sensor_log_{t:%Y-%m}.csv", SN_COLS, sn_rows)
    print(f"Logged {len(wb_rows)} waterbodies, {len(sn_rows)} sensors, "
          f"{len(adv_rows)} active advisories.")

    snapshot_model_config(waterbodies, t)
    archive_site_js()


if __name__ == "__main__":
    main()
