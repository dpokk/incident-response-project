"""Simple threshold detector: decides *that* something is wrong (the investigation decides *why*)."""
from datetime import datetime

from .analysis import pod_limits
from .collector import metric_queries


class Detector:
    def __init__(self, settings, prom, kube):
        self.s, self.prom, self.kube = settings, prom, kube
        self.q = metric_queries(settings.namespace, settings.entry_app)
        self.prev_restarts: dict[str, int] = {}
        self.streak = {"errors": 0, "cpu": 0}

    def sample(self) -> dict:
        pods = self.kube.pods(self.s.namespace)
        limits = {p["name"]: pod_limits(p["containers"], "cpu_limit_cores", "memory_limit_bytes") for p in pods}
        cpu, mem = {}, {}
        for r in self.prom.query(self.q["cpu_cores_by_pod"]):
            lim = limits.get(r["labels"].get("pod"), {}).get("cpu")
            if lim:
                cpu[r["labels"]["pod"]] = r["value"] / lim
        for r in self.prom.query(self.q["memory_working_set_by_pod"]):
            lim = limits.get(r["labels"].get("pod"), {}).get("mem")
            if lim:
                mem[r["labels"]["pod"]] = r["value"] / lim
        restarts = {p["name"]: sum(c["restart_count"] for c in p["containers"]) for p in pods}
        new_restarts = {n: v - self.prev_restarts[n] for n, v in restarts.items()
                        if n in self.prev_restarts and v > self.prev_restarts[n]}
        self.prev_restarts = restarts
        return {
            "t": self.prom.now(),
            "rps": self.prom.scalar(self.q["entry_rps"], 0.0),
            "error_ratio": self.prom.scalar(self.q["entry_error_ratio"], 0.0),
            "p95": self.prom.scalar(self.q["entry_p95_latency_s"], None),
            "cpu": cpu, "mem": mem, "restarts": restarts, "new_restarts": new_restarts,
            "pods": pods,
            "not_ready": [p["name"] for p in pods if not p["ready"]],
        }

    def evaluate(self, s: dict) -> list[str]:
        """Return trigger descriptions for this sample (empty list = healthy)."""
        triggers = []
        self.streak["errors"] = self.streak["errors"] + 1 if s["error_ratio"] >= self.s.error_rate_threshold else 0
        if self.streak["errors"] >= 2:
            triggers.append(f"HTTP 5xx rate {s['error_ratio'] * 100:.1f}% > {self.s.error_rate_threshold * 100:.0f}% "
                            f"at {self.s.entry_app}")
        for pod, n in s["new_restarts"].items():
            triggers.append(f"pod {pod} restarted (+{n})")
        hot_cpu = {p: r for p, r in s["cpu"].items() if r >= self.s.cpu_threshold}
        self.streak["cpu"] = self.streak["cpu"] + 1 if hot_cpu else 0
        if self.streak["cpu"] >= 2:
            p, r = max(hot_cpu.items(), key=lambda x: x[1])
            triggers.append(f"CPU {r * 100:.0f}% of limit on {p}")
        hot_mem = {p: r for p, r in s["mem"].items() if r >= self.s.memory_threshold}
        if hot_mem:
            p, r = max(hot_mem.items(), key=lambda x: x[1])
            triggers.append(f"memory {r * 100:.0f}% of limit on {p}")
        return triggers


def format_sample(s: dict) -> str:
    t = datetime.fromtimestamp(s["t"]).astimezone().strftime("%H:%M:%S")
    p95 = f"{s['p95'] * 1000:.0f}ms" if s["p95"] is not None else "n/a"
    pods = " ".join(
        f"{p['name'].split('-')[0]}-{p['name'].split('-')[-1]}[cpu {s['cpu'].get(p['name'], 0) * 100:.0f}% "
        f"mem {s['mem'].get(p['name'], 0) * 100:.0f}% rst {s['restarts'].get(p['name'], 0)}{'' if p['ready'] else ' NOTREADY'}]"
        for p in s["pods"])
    return f"{t}  rps {s['rps']:6.1f}  5xx {s['error_ratio'] * 100:5.1f}%  p95 {p95:>7}  {pods}"
