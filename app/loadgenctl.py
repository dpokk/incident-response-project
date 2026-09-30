"""Tiny CLI for the load generator's control API; run inside the loadgen pod via kubectl exec.

  python loadgenctl.py status
  python loadgenctl.py spike --peak 800 --ramp 10 --hold 90 --rampdown 5
  python loadgenctl.py stop
  python loadgenctl.py base --rps 100
"""
import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8089"


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        print(json.dumps(json.loads(resp.read()), indent=2))


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("stop")
    s = sub.add_parser("spike")
    s.add_argument("--peak", type=float, default=800)
    s.add_argument("--ramp", type=float, default=10)
    s.add_argument("--hold", type=float, default=90)
    s.add_argument("--rampdown", type=float, default=5)
    b = sub.add_parser("base")
    b.add_argument("--rps", type=float, required=True)
    o = sub.add_parser("order", help="send one order through the frontend, like a single user would")
    o.add_argument("--quantity", type=int, default=1)
    o.add_argument("--total", type=float, default=10.0)
    a = p.parse_args()
    if a.cmd == "order":
        url = os.getenv("TARGET_URL", "http://frontend.shop.svc.cluster.local:8080/api/orders")
        query = urllib.parse.urlencode({"quantity": a.quantity, "total": a.total})
        try:
            with urllib.request.urlopen(f"{url}?{query}", timeout=10) as resp:
                print(resp.status, resp.read().decode())
        except urllib.error.HTTPError as exc:
            print(exc.code, exc.read().decode())
        return
    if a.cmd == "status":
        call("GET", "/status")
    elif a.cmd == "stop":
        call("POST", "/stop", {})
    elif a.cmd == "spike":
        call("POST", "/spike", {"peak_rps": a.peak, "ramp_s": a.ramp, "hold_s": a.hold, "rampdown_s": a.rampdown})
    elif a.cmd == "base":
        call("POST", "/base", {"rps": a.rps})


if __name__ == "__main__":
    main()
