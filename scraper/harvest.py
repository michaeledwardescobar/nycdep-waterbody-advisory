import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

# Rainfall history harvester, v3.
# Designed to survive interruption:
#   - commits+pushes after EVERY gauge (a timeout can only lose the
#     gauge currently in progress, never finished ones)
#   - stops itself gracefully before the workflow timeout
#   - keeps a completeness marker per gauge, so a resumed run knows
#     whether it may stop at already-seen data (gauge complete) or must
#     keep digging past it (gauge was interrupted)
#   - probes larger page sizes and verifies hour-to-hour continuity
#     before trusting them; falls back to the dashboard's 96 on doubt

BASE = "https://nycwaterbodyadvisory.azurewebsites.net/api"
HDRS = {"User-Agent": "Mozilla/5.0 (personal research logger)",
        "Accept": "application/json, text/plain, */*"}
DEFAULT_PAGE_SIZE = 96
CANDIDATE_SIZES = [768, 384, 96]
MAX_PAGES = 30000
SLEEP = 0.1
TIME_BUDGET_SEC = int(os.environ.get("HARVEST_BUDGET_SEC", 280 * 60))
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "rain_history"
META = OUT_DIR / "_meta.json"
T0 = time.monotonic()


def out_of_time():
    return time.monotonic() - T0 > TIME_BUDGET_SEC


def get(path, params, tries=3):
    for i in range(tries):
        try:
            r = requests.get(f"{BASE}/{path}", params=params,
                             headers=HDRS, timeout=30)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  [retry {i+1}] {e}")
            time.sleep(2 * (i + 1))
    return None


def as_record_list(data):
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for k in ("items", "results", "data", "records", "values"):
            if isinstance(data.get(k), list):
                return [d for d in data[k] if isinstance(d, dict)]
    return []


def rec_key(d):
    rid = d.get("id")
    if isinstance(rid, (str, int)):
        return str(rid)
    return f"{d.get('sensorId')}-{d.get('occurredOn')}"


def parse_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", ""))
    except ValueError:
        return None


def fetch_page(sensor_id, page, size):
    data = get("rainfall-samples",
               {"page": page, "pageSize": size, "sensorId": sensor_id})
    return None if data is None else as_record_list(data)


def choose_page_size(sensor_id):
    """Pick the largest page size the server demonstrably honors.
    Honors = returns the full count AND page1 continues exactly one
    hour after page0 ends (records run newest -> oldest)."""
    for size in CANDIDATE_SIZES:
        if size == DEFAULT_PAGE_SIZE:
            return size  # the dashboard's own size; trusted baseline
        p0 = fetch_page(sensor_id, 0, size)
        if not p0 or len(p0) != size:
            continue
        p1 = fetch_page(sensor_id, 1, size)
        if not p1:
            continue
        t_last = parse_ts(p0[-1].get("occurredOn"))
        t_next = parse_ts(p1[0].get("occurredOn"))
        if t_last and t_next and (t_last - t_next) == timedelta(hours=1):
            print(f"[{sensor_id}] server honors pageSize={size}")
            return size
        print(f"[{sensor_id}] pageSize={size} failed continuity check")
    return DEFAULT_PAGE_SIZE


def load_meta():
    if META.exists():
        return json.loads(META.read_text())
    return {}


def save_meta(meta):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    META.write_text(json.dumps(meta, indent=1, sort_keys=True))


def git(*args):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                       text=True)
    if r.returncode != 0:
        print(f"  [git {' '.join(args[:2])}] {r.stderr.strip()[:200]}")
    return r.returncode == 0


def checkpoint(msg):
    """Commit and push whatever is harvested so far. Best-effort:
    a failed push never aborts the harvest."""
    git("add", "-A", str(OUT_DIR.relative_to(ROOT)))
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT
                      ).returncode == 0:
        return
    if git("commit", "-m", msg):
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                cwd=ROOT, capture_output=True,
                                text=True).stdout.strip()
        git("pull", "--rebase", "origin", branch)
        git("push", "origin", branch)
        print(f"  [checkpoint] {msg}")


def harvest_sensor(sensor_id, meta):
    out = OUT_DIR / f"{sensor_id}.csv"
    m = meta.get(sensor_id, {})
    complete = bool(m.get("complete"))

    merged = {}
    if out.exists():
        with out.open() as f:
            for row in csv.DictReader(f):
                merged[row["id"]] = row
    if merged:
        state = "complete" if complete else "INCOMPLETE"
        print(f"[{sensor_id}] resuming ({state}); "
              f"{len(merged)} records saved")

    size = choose_page_size(sensor_id)
    new = 0
    reached_end = False

    for page in range(MAX_PAGES):
        if out_of_time():
            print(f"[{sensor_id}] time budget reached at page {page}; "
                  "stopping gracefully (resume will continue)")
            break
        data = fetch_page(sensor_id, page, size)
        if data is None:
            print(f"[{sensor_id}] giving up on page {page} after retries")
            break
        if not data:
            reached_end = True
            print(f"[{sensor_id}] end of history at page {page}")
            break
        if page == 0:
            print(f"[{sensor_id}] sample record: "
                  f"{json.dumps(data[0])[:250]}")
        fresh = 0
        for d in data:
            k = rec_key(d)
            if k not in merged:
                merged[k] = {"id": k, "sensor_id": d.get("sensorId"),
                             "occurred_on": d.get("occurredOn"),
                             "precip_in": d.get("precipitation")}
                fresh += 1
        new += fresh
        # Only a COMPLETE gauge may stop early at familiar data; an
        # interrupted gauge must dig through it to the far side.
        if fresh == 0 and complete:
            reached_end = True
            print(f"[{sensor_id}] caught up (page {page}); still complete")
            break
        if page % 25 == 0:
            print(f"[{sensor_id}] page {page}: oldest so far "
                  f"{data[-1].get('occurredOn')} ({len(merged)} records)")
        time.sleep(SLEEP)

    if new:
        rows = sorted(merged.values(), key=lambda r: r["occurred_on"])
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["id", "sensor_id",
                                              "occurred_on", "precip_in"])
            w.writeheader()
            w.writerows(rows)
        print(f"[{sensor_id}] wrote {len(rows)} records "
              f"({rows[0]['occurred_on']} -> {rows[-1]['occurred_on']})")

    meta[sensor_id] = {
        "complete": reached_end or complete,
        "records": len(merged),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    save_meta(meta)
    checkpoint(f"harvest: {sensor_id} "
               f"({'complete' if meta[sensor_id]['complete'] else 'partial'}, "
               f"{len(merged)} records)")
    return new


def main():
    sensors = get("sensors", {})
    if not sensors:
        sys.exit("Could not fetch sensor list.")
    ids = [s["id"] for s in sensors]
    meta = load_meta()
    # Incomplete/unstarted gauges first, completed ones topped up last
    ids.sort(key=lambda s: bool(meta.get(s, {}).get("complete")))
    print(f"Harvesting {len(ids)} gauges (incomplete first): {ids}")
    total = 0
    for sid in ids:
        if out_of_time():
            print("Time budget exhausted; remaining gauges next run.")
            break
        total += harvest_sensor(sid, meta)
    done = sum(1 for s in ids if meta.get(s, {}).get("complete"))
    print(f"Done. {total} new records. "
          f"{done}/{len(ids)} gauges complete. "
          f"{'RUN AGAIN to continue.' if done < len(ids) else 'ALL COMPLETE.'}")


if __name__ == "__main__":
    main()
