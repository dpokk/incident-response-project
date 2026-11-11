"""False-alarm measurement against the running demo: an idle period, then short stalls of both backend pods.
For each period: was the system seen unhealthy, peak 5xx ratio, peak error lines, and did an incident open."""
import json
import subprocess
import sys
import time

import requests

BASE = "http://127.0.0.1:8800"
OUT = sys.argv[1]
IDLE_S = int(sys.argv[2]) if len(sys.argv) > 2 else 600
STALLS = [3, 3, 3, 3, 3, 6, 6]
GAP_S = 60


def snap():
    return requests.get(f"{BASE}/api/snapshot", timeout=10).json()


def observe(seconds):
    """Sample every 2 s; return what was seen."""
    seen = {"unhealthy_samples": 0, "samples": 0, "peak_5xx": 0.0, "peak_error_lines": 0, "incidents": []}
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            s = snap()
        except requests.RequestException:
            time.sleep(2)
            continue
        y = s.get("system") or {}
        seen["samples"] += 1
        seen["unhealthy_samples"] += 1 if y.get("unhealthy") else 0
        seen["peak_5xx"] = max(seen["peak_5xx"], (y.get("traffic") or {}).get("error_ratio") or 0.0)
        seen["peak_error_lines"] = max(seen["peak_error_lines"], sum((y.get("errors") or {}).values()))
        inc = s.get("incident") or {}
        if s["phase"] not in ("monitoring", "stopped") and inc.get("id") and inc["id"] not in [i["id"] for i in seen["incidents"]]:
            seen["incidents"].append({"id": inc["id"], "signals": [x["text"] for x in inc.get("signals", [])]})
        time.sleep(2)
    return seen


def back_to_monitoring():
    s = snap()
    if s["phase"] == "monitoring":
        return
    while snap()["phase"] in ("detected", "investigating", "agent", "remediating", "auto_remediating"):
        time.sleep(5)
    requests.post(f"{BASE}/api/reset", timeout=10)
    while not (snap()["phase"] == "monitoring" and not snap()["busy"]):
        time.sleep(3)
    time.sleep(70)                                           # the reset's own 60 s pause


def stall(seconds):
    cmd = (f"sudo kill -STOP $(pgrep -f '[b]ackend.py'); sleep {seconds}; "
           f"sudo kill -CONT $(pgrep -f '[b]ackend.py')")
    subprocess.run(["minikube", "-p", "incident-demo", "ssh", "--", cmd], capture_output=True, timeout=120)


results = {"idle": None, "stalls": []}
results["idle"] = {"seconds": IDLE_S, **observe(IDLE_S)}
json.dump(results, open(OUT, "w"), indent=1)
back_to_monitoring()
for n, secs in enumerate(STALLS, 1):
    t = time.strftime("%H:%M:%S")
    stall(secs)
    r = {"n": n, "stall_s": secs, "at": t, **observe(GAP_S)}
    results["stalls"].append(r)
    json.dump(results, open(OUT, "w"), indent=1)
    if r["incidents"]:
        back_to_monitoring()
print(json.dumps(results, indent=1))
