"""Detection: notices *that* something is wrong. It reports symptoms, never causes. Provider-independent.

Signal sources (all generic, none tied to a specific failure), all obtained through capabilities:
  * resource state     - process restarts, processes stuck waiting (crash-loop back-off, image or config
                         problems), instances NotReady beyond a grace period, unschedulable instances
  * synthetic probe    - a user-like request to the entry service (`probe_request`)
  * application logs   - a burst of error-level log lines from any component (`get_logs`)
  * metrics (optional) - the user-facing HTTP 5xx ratio (`get_metrics`) when a metrics provider exists
"""
import time
from collections import defaultdict
from datetime import datetime

from . import logparse
from .capabilities.base import MetricsProvider, ResourceProvider, TimeRange


class Detector:
    def __init__(self, settings, resources: ResourceProvider, metrics: MetricsProvider | None = None,
                 clock=time.time):
        self.s, self.r, self.metrics, self.clock = settings, resources, metrics, clock
        self.prev_restarts: dict[tuple, int] = {}
        self.streak: dict[tuple, int] = defaultdict(int)
        self.primed = False

    def sample(self) -> dict:
        now = self.clock()
        since = int(self.s.poll_interval_s) + 1
        tr = TimeRange(now - since, now)
        self.r.reset()
        states = {c: self.r.get_resource_state(c, tr) for c in self.r.list_components()}
        states = {c: rs for c, rs in states.items() if rs is not None}
        signals, sustained = [], []

        for c, rs in states.items():
            for inst in rs.instances:
                for p in inst.processes:
                    key = (inst.name, p.name)
                    if self.primed and key in self.prev_restarts and p.restarts > self.prev_restarts[key]:
                        t = p.last_termination
                        signals.append(_sig("container_restart", c, now,
                                            f"Container {p.name} in {inst.kind.lower()} {inst.name} restarted "
                                            f"(restart #{p.restarts}; previous instance ended with reason="
                                            f"{t.reason if t else None}, exit code {t.exit_code if t else None})"))
                    self.prev_restarts[key] = p.restarts
                    if p.state == "waiting" and p.waiting_cause:
                        signals.append(_sig("container_waiting", c, now,
                                            f"Container {p.name} in {inst.kind.lower()} {inst.name} is waiting: "
                                            f"{p.waiting_reason}"))
                if inst.phase == "Running" and not inst.ready and inst.ready_since \
                        and now - inst.ready_since > self.s.not_ready_grace_s:
                    signals.append(_sig("pod_not_ready", c, now,
                                        f"{inst.kind} {inst.name} has been NotReady for {now - inst.ready_since:.0f}s"))
                if inst.unschedulable:
                    signals.append(_sig("pod_unschedulable", c, now, f"{inst.kind} {inst.name} cannot be scheduled"))
        self.primed = True

        # Synthetic user request through the entry service
        ok = total = 0
        last = None
        for _ in range(3):
            res = self.r.probe_request(self.s.entry_service, self.s.entry_port, self.s.entry_path)
            total += 1
            ok += 1 if 0 < res.status < 500 else 0
            last = (res.status, res.body)
        if 1 - ok / total >= self.s.probe_failure_threshold:
            sustained.append(_sig("entry_probe_failure", self.s.entry_service, now,
                                  f"Synthetic requests to {self.s.entry_service}{self.s.entry_path.split('?')[0]} failing: "
                                  f"{total - ok}/{total} failed (last: HTTP {last[0]} {last[1].strip()[:80]})"))

        # Burst of error-level log lines per component since the last poll
        errors = defaultdict(int)
        for c, rs in states.items():
            for inst in rs.instances:
                for p in inst.processes:
                    if p.state != "running":
                        continue
                    recs = logparse.parse_records(self.r.get_logs(c, inst.name, p.name, tr))
                    errors[c] += sum((r["count"] if isinstance(r.get("count"), int) else 1) for r in recs
                                     if (r.get("level") or "").lower() in ("error", "critical", "fatal")
                                     and (r["_t"] is None or r["_t"] >= now - since))
        for c, n in errors.items():
            if n >= self.s.error_log_threshold:
                sustained.append(_sig("error_logs", c, now, f"{c} logged {n} error-level lines in the last {since}s"))

        if self.metrics is not None:
            try:
                series = self.metrics.get_metrics("error_ratio", TimeRange(now - 30, now), self.s.entry_app)
                ratio = series[0].points[-1][1] if series and series[0].points else 0.0
                if ratio >= self.s.error_rate_threshold:
                    sustained.append(_sig("http_5xx_ratio", self.s.entry_app, now,
                                          f"HTTP 5xx ratio at {self.s.entry_app} is {ratio:.0%} ({self.metrics.name})"))
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
        return {"t": now, "signals": signals, "states": states, "probe": {"ok": ok, "total": total, "last": last},
                "errors": dict(errors), "unhealthy": bool(signals or sustained)}


def _sig(kind: str, subject: str, t: float, text: str) -> dict:
    return {"kind": kind, "subject": f"workload/{subject}", "t": t, "text": text}


def format_sample(s: dict) -> str:
    t = datetime.fromtimestamp(s["t"]).astimezone().strftime("%H:%M:%S")
    pr = s["probe"]
    probe = f"probe {pr['ok']}/{pr['total']} ok" + (f" (HTTP {pr['last'][0]})" if pr["ok"] < pr["total"] and pr["last"] else "")
    parts = []
    for c, rs in s["states"].items():
        for inst in rs.instances:
            waiting = "".join(f" {p.waiting_reason}" for p in inst.processes if p.state == "waiting")
            parts.append(f"{c}-{inst.name[-5:]}[{'ready' if inst.ready else 'NOTREADY'} rst={inst.restarts}{waiting}]")
    errs = " ".join(f"{k}:{v}" for k, v in s["errors"].items() if v)
    return f"{t}  {probe}  errors[{errs or '-'}]  {' '.join(parts)}"
