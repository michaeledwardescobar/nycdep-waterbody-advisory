import csv
import json
import sys
import time
from pathlib import Path
import requests

# One-time harvester: walk /api/rainfall-samples backward for every
# gauge until the server returns an empty page, saving hourly records
# to data/rain_history/<sensor>.csv. Resumable: reruns skip pages whose
# records are already saved and stop early when a full page of
# duplicates is hit (history already complete).

BASE = "https://nycwaterbodyadvisory.azurewebsites.net/api"
HDRS = {"User-Agent": "Mozilla/5.0 (personal research logger)",
        "Accept": "application/json, text/plain, */*"}
PAGE_SIZE = 96          # the page size the dashboard itself uses
MAX_PAGES = 3000        # hard stop â‰ˆ 33 years/sensor; safety valve
SLEEP = 0.15            # politeness delay between requests
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "rain_history"


def get(url, params, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=HDRS, timeout=30)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  [retry {i+1}] {e}")
            time.sleep(2 * (i + 1))
    return None


def rec_key(d):
    """Stable unique key per hourly record, robust to whatever shape
    the server gives the 'id' field (string, number, or nested object)."""
    rid = d.get("id")
    if isinstance(rid, (str, int)):
        return str(rid)
    return f"{d.get('sensorId')}-{d.get('occurredOn')}"


def as_record_list(data):
    """The API may return a bare list or wrap it in an envelope."""
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for k in ("items", "results", "data", "records", "values"):
            if isinstance(data.get(k), list):
                return [d for d in data[k] if isinstance(d, dict)]
    return []


def harvest_sensor(sensor_id):
    out = OUT_DIR / f"{sensor_id}.csv"
    seen = set()
    if out.exists():
        with out.open() as f:
            seen = {row["id"] for row in csv.DictReader(f)}
        print(f"[{sensor_id}] resuming; {len(seen)} records already saved")

    new_rows = []
    for page in range(MAX_PAGES):
        data = get(f"{BASE}/rainfall-samples",
                   {"page": page, "pageSize": PAGE_SIZE,
                    "sensorId": sensor_id})
        if data is None:
            print(f"[{sensor_id}] giving up on page {page} after retries")
            break
        data = as_record_list(data)
        if not data:
            print(f"[{sensor_id}] end of history at page {page}")
            break
        if page == 0:
            print(f"[{sensor_id}] sample record: "
                  f"{json.dumps(data[0])[:300]}")
        fresh = [d for d in data if rec_key(d) not in seen]
        for d in fresh:
            seen.add(rec_key(d))
        new_rows.extend(fresh)
        if not fresh:
            print(f"[{sensor_id}] page {page} all duplicates; "
                  "history already complete")
            break
        if page % 25 == 0:
            print(f"[{sensor_id}] page {page}: "
                  f"oldest so far {data[-1].get('occurredOn')}")
        time.sleep(SLEEP)

    if not new_rows:
        return 0

    # Merge, sort ascending by timestamp, rewrite the file
    merged = {}
    if out.exists():
        with out.open() as f:
            for row in csv.DictReader(f):
                merged[row["id"]] = row
    for d in new_rows:
        merged[rec_key(d)] = {
            "id": rec_key(d),
            "sensor_id": d.get("sensorId"),
            "occurred_on": d.get("occurredOn"),
            "precip_in": d.get("precipitation"),
        }
    rows = sorted(merged.values(), key=lambda r: r["occurred_on"])
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "sensor_id",
                                          "occurred_on", "precip_in"])
        w.writeheader()
        w.writerows(rows)
    print(f"[{sensor_id}] wrote {len(rows)} total records "
          f"({rows[0]['occurred_on']} -> {rows[-1]['occurred_on']})")
    return len(new_rows)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sensors = get(f"{BASE}/sensors", {})
    if not sensors:
        sys.exit("Could not fetch sensor list.")
    ids = [s["id"] for s in sensors]
    print(f"Harvesting {len(ids)} gauges: {ids}")
    total = 0
    for sid in ids:
        total += harvest_sensor(sid)
    print(f"Done. {total} new records this run.")


if __name__ == "__main__":
    main()
