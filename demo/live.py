"""Live system view: component status from the detector's own sample, traffic from Prometheus, and live log lines.

Nothing here is computed for show. Status comes from the same Detector sample that opens incidents, so what the
page shows is exactly what detection sees. Logs are the containers' real output (kubectl-logs equivalent).
"""
import json
import threading
import time
from collections import deque

from investigator.capabilities.base import TimeRange
from investigator.kube import Kube

NAMESPACES = ("shop", "loadtest")
LOG_FIELDS = ("requests", "errors_5xx", "error_rate", "interval_s",
              "route", "status", "code", "rps", "inflight", "db_state", "db_host", "db_port", "error", "last_error",
              "reason", "count", "target_rps", "outcomes_10s", "cgroup_mem_mb", "mem_limit_ratio", "upstream",
              "sample_error", "exc_type", "quantity", "order_id")
MAX_LINES_PER_TICK = 40                     # per container; anything above is counted, never silently dropped


def component_status(sample: dict, error_threshold: float = 5) -> list[dict]:
    """One row per component, from the detector sample's ResourceState records. A component whose instances are
    'ready' but which logs errors above the detector's own threshold is shown as degraded, not healthy."""
    out = []
    errors = sample.get("errors") or {}
    for name, rs in sorted(sample["states"].items()):
        insts = []
        for i in rs.instances:
            proc = i.processes[0] if i.processes else None
            insts.append({"name": i.name, "ready": i.ready, "phase": i.phase, "restarts": i.restarts,
                          "waiting": proc.waiting_reason if proc and proc.state == "waiting" else None,
                          "last_termination": (proc.last_termination.reason if proc and proc.last_termination
                                               else None),
                          "memory_limit": (rs.limits.get(proc.name) or {}).get("memory") if proc else None})
        if rs.desired == 0:
            status = "down"
        elif rs.ready >= rs.desired and all(x["ready"] and not x["waiting"] for x in insts):
            status = "healthy"
        elif rs.ready == 0:
            status = "down"
        else:
            status = "degraded"
        note = None
        if status == "healthy" and errors.get(name, 0) >= error_threshold:
            status, note = "degraded", f"{errors[name]} error log lines in the last poll"
        out.append({"name": name, "kind": rs.kind, "desired": rs.desired, "ready": rs.ready, "status": status,
                    "note": note, "instances": insts})
    return out


def traffic(metrics, target: str, now: float) -> dict:
    """Request rate and 5xx ratio at the entry component, from the metrics provider (None if unavailable)."""
    out = {"request_rate": None, "error_ratio": None, "source": metrics.name if metrics else None}
    if metrics is None:
        return out
    for key in ("request_rate", "error_ratio"):
        try:
            series = metrics.get_metrics(key, TimeRange(now - 60, now), target=target)
            pts = [p for s in series or [] for p in s.points]
            out[key] = float(pts[-1][1]) if pts else None
        except Exception:  # noqa: BLE001 - metrics are optional
            pass
    return out


def format_line(app: str, raw: str) -> tuple[str, str]:
    """(level, text) for one raw log line: JSON lines keep their message and the interesting fields."""
    try:
        rec = json.loads(raw)
        if not isinstance(rec, dict):
            raise ValueError
    except ValueError:
        up = raw.upper()
        level = "error" if any(w in up for w in ("ERROR", "FATAL", "PANIC", "TRACEBACK")) else \
            ("warning" if "WARN" in up else "info")
        return level, raw.strip()[:300]
    level = str(rec.get("level") or "info").lower()
    extras = []
    for k in LOG_FIELDS:
        if k in rec and rec[k] not in (None, "", {}):
            v = rec[k]
            if isinstance(v, dict):
                v = " ".join(f"{a}={b}" for a, b in v.items())
            extras.append(f"{k}={str(v)[:90]}")
    return level, (str(rec.get("msg") or "").strip() + ("  " + " ".join(extras) if extras else ""))[:300]


class LogTailer(threading.Thread):
    """Polls every container's recent log lines and publishes the new ones."""

    def __init__(self, bus, context: str, interval: float = 2.0):
        super().__init__(daemon=True, name="log-tailer")
        self.bus, self.kube, self.interval = bus, Kube(context), interval
        self.last: dict[tuple, float] = {}
        self.recent: deque = deque(maxlen=250)     # for pages that connect later
        self.running = threading.Event()

    def run(self) -> None:
        while True:
            if self.running.is_set():
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 - keep tailing through API hiccups
                    self.bus.publish("log", {"lines": [{"t": time.time(), "app": "demo", "pod": "-", "level": "warning",
                                                        "text": f"log read failed: {type(exc).__name__}"}]}, keep=False)
            time.sleep(self.interval)

    def tick(self) -> None:
        lines = []
        for ns in NAMESPACES:
            for pod in self.kube.pods(ns):
                if pod.get("deleted"):
                    continue
                for c in pod.get("containers", []):
                    key = (ns, pod["name"], c["name"])
                    since = self.last.get(key)
                    got = self.kube.logs(ns, pod["name"], c["name"], since_s=6 if since else 4, tail=400)
                    new = [(t, raw) for t, raw in got if t is not None and (since is None or t > since)]
                    if not new:
                        continue
                    self.last[key] = max(t for t, _ in new)
                    dropped = max(0, len(new) - MAX_LINES_PER_TICK)
                    for t, raw in new[-MAX_LINES_PER_TICK:]:
                        level, text = format_line(pod.get("app") or c["name"], raw)
                        lines.append({"t": t, "app": pod.get("app") or c["name"], "pod": pod["name"],
                                      "level": level, "text": text})
                    if dropped:
                        lines.append({"t": new[-1][0], "app": pod.get("app") or c["name"], "pod": pod["name"],
                                      "level": "info", "text": f"… {dropped} more line(s) in this interval not shown"})
        if lines:
            lines.sort(key=lambda x: x["t"])
            self.recent.extend(lines)
            self.bus.publish("log", {"lines": lines}, keep=False)
