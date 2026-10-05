# backend/tests/perf_profile.py
"""
Read-only performance profile of the endpoints the Portfolio and Data &
Reports pages call. Counts and times every Supabase HTTP request each
endpoint makes, cold (cache empty) and warm (cache filled), plus a
"page load" run where the Portfolio page's calls fire at the same time,
like the browser does. Writes nothing to the database.

Run from backend/ with the venv active:
    python tests\\perf_profile.py
"""
import os, sys, time, threading, collections
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
from fastapi.testclient import TestClient

# ── record every outbound Supabase request ───────────────────────────
# Global (not thread-local): FastAPI runs sync endpoints on worker threads.
_calls: list = []
_rec = {"on": False}
_lock = threading.Lock()
_orig_send = httpx.Client.send
def _send(self, request, *a, **k):
    t = time.perf_counter()
    try:
        return _orig_send(self, request, *a, **k)
    finally:
        url = str(request.url)
        if "supabase" in url and _rec["on"]:
            path = request.url.path.replace("/rest/v1/", "")
            with _lock:
                _calls.append((path, time.perf_counter() - t))
httpx.Client.send = _send

from src.main import app
from src.services import supabase_service as sb

client = TestClient(app)

ENDPOINTS = {
    "Portfolio": [
        "/api/portfolio/?time_range=SI",
        "/api/portfolio/fund_ledger",
        "/api/management/strategies",
        "/api/management/pods",
    ],
    "Data & Reports": [
        "/api/data-feeds",
    ],
    "Other pages": [
        "/api/portfolio/hierarchy/strategy",
        "/api/portfolio/hierarchy/pod",
        "/api/gtx/state",
    ],
}

def hit(path):
    with _lock:
        _calls.clear()
    _rec["on"] = True
    t = time.perf_counter()
    r = client.get(path)
    dt = time.perf_counter() - t
    _rec["on"] = False
    with _lock:
        calls = list(_calls)
    return r.status_code, dt, calls

def report(label, path, code, dt, calls):
    db = sum(c[1] for c in calls)
    dup = [(p, n) for p, n in collections.Counter(c[0] for c in calls).items() if n > 1]
    print(f"  {label:<5} {code} {dt*1000:8.0f} ms  | {len(calls):3d} DB calls, {db*1000:7.0f} ms in DB | {path}")
    return dup

print("\n=== COLD vs WARM, one request at a time ===")
slow = collections.defaultdict(float)
for page, paths in ENDPOINTS.items():
    print(f"\n[{page}]")
    for p in paths:
        sb.invalidate_all_cache()
        code, dt, calls = hit(p)
        dup = report("cold", p, code, dt, calls)
        for path, d in calls:
            slow[path] += d
        code, dt2, calls2 = hit(p)
        report("warm", p, code, dt2, calls2)
        if dup:
            print("        repeated table reads (cold):", ", ".join(f"{t} x{n}" for t, n in sorted(dup, key=lambda x: -x[1])[:6]))

print("\n=== Slowest tables overall (cold, summed) ===")
for path, d in sorted(slow.items(), key=lambda x: -x[1])[:12]:
    print(f"  {d*1000:7.0f} ms  {path}")

print("\n=== Portfolio page load: its calls fired together, cache empty ===")
sb.invalidate_all_cache()
t = time.perf_counter()
with _lock:
    _calls.clear()
_rec["on"] = True
def timed(path):
    t0 = time.perf_counter(); r = client.get(path); return r.status_code, time.perf_counter() - t0
with ThreadPoolExecutor(4) as ex:
    res = list(ex.map(timed, ENDPOINTS["Portfolio"]))
wall = time.perf_counter() - t
_rec["on"] = False
for p, (code, dt) in zip(ENDPOINTS["Portfolio"], res):
    print(f"  {code} {dt*1000:8.0f} ms  {p}")
cnt = collections.Counter(c[0] for c in _calls)
print(f"  page ready after {wall*1000:.0f} ms;  total DB calls {len(_calls)} ({len(cnt)} distinct tables)")
dups = [(t, n) for t, n in cnt.items() if n > 1]
if dups:
    print("  same table fetched more than once while loading together:", ", ".join(f"{t} x{n}" for t, n in sorted(dups, key=lambda x: -x[1])))
