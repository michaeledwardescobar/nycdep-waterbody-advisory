import csv, json, sys
from datetime import datetime, timezone
from pathlib import Path
import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"
HDRS = {"User-Agent": "Mozilla/5.0 (personal research logger)",
        "Accept": "application/json, text/plain, */*"}
COLS = ["poll_time_utc", "waterbody", "on_advisory",
        "advisory_until", "advisory_hours_left", "sensor_id",
        "rain_24h_in", "source_endpoint"]

def get_first(d, *names):
    low = {k.lower(): v for k, v in d.items()} if isinstance(d, dict) else {}
    for n in names:
        cur, ok = low, True
        for p in n.lower().split("."):
            if isinstance(cur, dict):
                cur = {k.lower(): v for k, v in cur.items()}
            if not isinstance(cur, dict) or p not in cur:
                ok = False
                break
            cur = cur[p]
        if ok and cur is not None:
            return cur
    return None

def walk(node):
    if isinstance(node, dict):
        keys = {k.lower() for k in node}
        if "name" in keys and (keys & {"advisory", "sensor",
                "onadvisory", "advisoryuntil", "hours", "status"}):
            yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for x in node:
            yield from walk(x)

def main():
    t = datetime.now(timezone.utc)
    eps_file = HERE / "endpoints.json"
    if not eps_file.exists():
        sys.exit("endpoints.json missing - run discover.py")
    eps = json.loads(eps_file.read_text())["endpoints"]
    rows = []
    raw_dir = DATA / "raw" / f"{t:%Y-%m}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for i, ep in enumerate(eps):
        try:
            r = requests.get(ep, headers=HDRS, timeout=30)
            r.raise_for_status()
            payload = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"[warn] {ep}: {e}")
            continue
        (raw_dir / f"{t:%Y%m%dT%H%M%SZ}_{i}.json").write_text(
            json.dumps(payload, indent=1))
        for wb in walk(payload):
            sensor = get_first(wb, "sensor") or {}
            until = get_first(wb, "advisory.until", "advisoryUntil",
                              "until")
            hours = get_first(wb, "advisory.hours", "advisoryHours",
                              "hours")
            on = get_first(wb, "onAdvisory", "advisory.active",
                           "isOnAdvisory")
            if on is None:
                on = bool(until) or (isinstance(hours, (int, float))
                                     and hours > 0)
            rows.append({
                "poll_time_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "waterbody": str(get_first(wb, "name") or "").strip().upper(),
                "on_advisory": bool(on),
                "advisory_until": until or "",
                "advisory_hours_left": hours if hours is not None else "",
                "sensor_id": get_first(wb, "sensor.id", "sensorId")
                    or (sensor.get("id") if isinstance(sensor, dict) else ""),
                "rain_24h_in": get_first(wb, "sensor.accumulated",
                    "accumulated", "rainfall", "rain24") or "",
                "source_endpoint": ep,
            })
    if not rows:
        print("No rows this run (raw JSON archived if any).")
        return
    out = DATA / f"advisory_log_{t:%Y-%m}.csv"
    new = not out.exists()
    with out.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"Appended {len(rows)} rows to {out.name}")

if __name__ == "__main__":
    main()
