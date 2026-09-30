"""Detection: notices *that* something is wrong. It reports symptoms, never causes.

Signal sources (all generic, none tied to a specific failure):
  * Kubernetes state   - container restarts, containers stuck waiting (CrashLoopBackOff, image pull,
                         config errors), pods NotReady beyond a grace period, unschedulable pods
  * synthetic probe    - a user-like request to the entry service through the API server proxy
  * application logs   - a burst of error-level log lines from any workload
  * metrics (optional) - HTTP 5xx ratio from Prometheus when it is available
"""
import time
from collections import defaultdict
from datetime import datetime

from . import logparse
from .collect import BAD_WAITING
from .kube import Kube


class Detector:
    def __init__(self, settings, kube: Kube, prom=None, clock=time.time):
        self.s, self.kube, self.prom, self.clock = settings, kube, prom, clock
        self.prev_restarts: dict[tuple, int] = {}
        self.streak: dict[str, int] = defaultdict(int)
        self.primed = False

    def sample(self) -> dict:
        now = self.clock()
        pods = self.kube.pods(self.s.namespace)
        signals, sustained = [], []

        for p in pods:
            wl = p["app"] or p["owner"] or p["name"]
            for c in p["containers"]:
                key = (p["name"], c["name"])
                if self.primed and key in self.prev_restarts and c["restart_count"] > self.prev_restarts[key]:
                    ls = c["last_state"] or {}
                    signals.append(_sig("container_restart", wl, now,
                                        f"Container {c['name']} in pod {p['name']} restarted (restart #{c['restart_count']}; "
                                        f"previous instance ended with reason={ls.get('reason')}, exit code {ls.get('exit_code')})"))
                self.prev_restarts[key] = c["restart_count"]
                st = c["state"] or {}
                if st.get("state") == "waiting" and st.get("reason") in BAD_WAITING:
                    signals.append(_sig("container_waiting", wl, now,
                                        f"Container {c['name']} in pod {p['name']} is waiting: {st['reason']}"))
            if p["phase"] == "Running" and not p["ready"] and p["ready_since"] and now - p["ready_since"] > self.s.not_ready_grace_s:
                signals.append(_sig("pod_not_ready", wl, now,
                                    f"Pod {p['name']} has been NotReady for {now - p['ready_since']:.0f}s"))
            if p["unschedulable"]:
                signals.append(_sig("pod_unschedulable", wl, now, f"Pod {p['name']} cannot be scheduled"))
        self.primed = True

        # Synthetic user request through the entry service
        ok = total = 0
        last = None
        for _ in range(3):
            status, body = self.kube.service_proxy_get(self.s.namespace, self.s.entry_service, self.s.entry_port,
                                                       self.s.entry_path, timeout=5)
            total += 1
            ok += 1 if 0 < status < 500 else 0
            last = (status, body)
        fail_ratio = 1 - ok / total
        if fail_ratio >= self.s.probe_failure_threshold:
            sustained.append(_sig("entry_probe_failure", self.s.entry_service, now,
                                  f"Synthetic requests to {self.s.entry_service}{self.s.entry_path.split('?')[0]} failing: "
                                  f"{total - ok}/{total} failed (last: HTTP {last[0]} {last[1].strip()[:80]})"))

        # Burst of error-level log lines per workload since the last poll
        errors = defaultdict(int)
        since = int(self.s.poll_interval_s) + 1
        for p in pods:
            for c in p["containers"]:
                if (c["state"] or {}).get("state") != "running":
                    continue
                recs = logparse.parse_records(self.kube.logs(self.s.namespace, p["name"], c["name"], since_s=since, tail=500))
                errors[p["app"] or p["name"]] += sum(
                    (r["count"] if isinstance(r.get("count"), int) else 1) for r in recs
                    if (r.get("level") or "").lower() in ("error", "critical", "fatal"))
        for wl, n in errors.items():
            if n >= self.s.error_log_threshold:
                sustained.append(_sig("error_logs", wl, now, f"{wl} logged {n} error-level lines in the last {since}s"))

        if self.prom is not None:
            try:
                q = (f'(sum(rate(http_requests_total{{namespace="{self.s.namespace}",app="{self.s.entry_app}",code=~"5.."}}[30s])) '
                     f'or vector(0)) / clamp_min(sum(rate(http_requests_total{{namespace="{self.s.namespace}",'
                     f'app="{self.s.entry_app}"}}[30s])), 0.001)')
                ratio = self.prom.scalar(q, 0.0) or 0.0
                if ratio >= self.s.error_rate_threshold:
                    sustained.append(_sig("http_5xx_ratio", self.s.entry_app, now,
                                          f"HTTP 5xx ratio at {self.s.entry_app} is {ratio:.0%} (Prometheus)"))
            except Exception:  # noqa: BLE001 - metrics are optional
                pass

        # Sustained signals must hold for two consecutive polls to avoid flapping on a single blip.
        kinds_now = {(x["kind"], x["subject"]) for x in sustained}
        for k in list(self.streak):
            if k not in kinds_now:
                self.streak[k] = 0
        for x in sustained:
            self.streak[(x["kind"], x["subject"])] += 1
            if self.streak[(x["kind"], x["subject"])] >= 2:
                signals.append(x)
        return {"t": now, "signals": signals, "pods": pods, "probe": {"ok": ok, "total": total, "last": last},
                "errors": dict(errors), "unhealthy": bool(signals or sustained)}


def _sig(kind: str, subject: str, t: float, text: str) -> dict:
    return {"kind": kind, "subject": f"workload/{subject}", "t": t, "text": text}


def format_sample(s: dict) -> str:
    t = datetime.fromtimestamp(s["t"]).astimezone().strftime("%H:%M:%S")
    pr = s["probe"]
    probe = f"probe {pr['ok']}/{pr['total']} ok" + (f" (HTTP {pr['last'][0]})" if pr["ok"] < pr["total"] and pr["last"] else "")
    pods = " ".join(
        f"{p['name'].rsplit('-', 2)[0] if p['owner'] else p['name']}-{p['name'][-5:]}"
        f"[{'ready' if p['ready'] else 'NOTREADY'} rst={sum(c['restart_count'] for c in p['containers'])}"
        + "".join(f" {(c['state'] or {}).get('reason')}" for c in p["containers"] if (c["state"] or {}).get("state") == "waiting")
        + "]" for p in s["pods"])
    errs = " ".join(f"{k}:{v}" for k, v in s["errors"].items() if v)
    return f"{t}  {probe}  errors[{errs or '-'}]  {pods}"
