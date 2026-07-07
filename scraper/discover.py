import json, re, sys
from pathlib import Path
from urllib.parse import urljoin
import requests

BASE = "https://nycwaterbodyadvisory.azurewebsites.net/"
OUT = Path(__file__).resolve().parent / "endpoints.json"
HDRS = {"User-Agent": "Mozilla/5.0 (personal research logger)",
        "Accept": "application/json, text/plain, */*"}
WORDS = ("advisory", "waterbody", "sensor", "accumulated", "rain")
FALLBACKS = ["api/waterbodies", "api/waterbody", "api/WaterBodies",
             "api/advisories", "api/Advisories", "api/sensors",
             "api/data", "Home/GetWaterBodies", "Home/GetData"]

def fetch(url, t=25):
    try:
        r = requests.get(url, headers=HDRS, timeout=t)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        print(f"  [skip] {url} -> {e}")
        return None

def url_candidates(text):
    c = set(m.rstrip("/") for m in
            re.findall(r'https?://[^\s"\'<>)]+', text))
    for m in re.findall(
            r'["\'](/?(?:api|Api|API|Home|data|services?)'
            r'[A-Za-z0-9_/\-.]*)["\']', text):
        c.add(urljoin(BASE, m))
    return c

def is_hit(r):
    if r is None:
        return False
    body = r.text.lstrip()
    if "json" not in r.headers.get("Content-Type", "") and \
       not body.startswith(("{", "[")):
        return False
    try:
        blob = json.dumps(r.json())[:20000].lower()
    except ValueError:
        return False
    return any(w in blob for w in WORDS)

def main():
    shell = fetch(BASE)
    if shell is None:
        sys.exit(1)
    cands = url_candidates(shell.text)
    for src in re.findall(
            r'<script[^>]+src=["\']([^"\']+)["\']', shell.text, re.I):
        js = fetch(urljoin(BASE, src))
        if js is not None:
            cands |= url_candidates(js.text)
    cands |= {urljoin(BASE, p) for p in FALLBACKS}
    cands = sorted(c for c in cands if "nycwaterbodyadvisory" in c)
    print(f"Probing {len(cands)} candidates...")
    hits = [u for u in cands if is_hit(fetch(u, 20))]
    if not hits:
        print("No endpoints found automatically. See README fallback.")
        sys.exit(2)
    OUT.write_text(json.dumps({"endpoints": hits}, indent=2))
    print(f"Saved {len(hits)} endpoint(s): {hits}")

if __name__ == "__main__":
    main()
