"""Optional metrics enrichment (Iteration 1's Prometheus). Adds facts; the investigation works without it."""
from statistics import median

from .evidence import EvidenceStore


def _q(ns: str, entry: str) -> dict:
    sel, esel = f'namespace="{ns}"', f'namespace="{ns}",app="{entry}"'
    pod = f'{sel},container="",pod!=""'  # pod-level cgroup series: always exported by cAdvisor
    total = f"sum(rate(http_requests_total{{{esel}}}[15s]))"
    errors = f'sum(rate(http_requests_total{{{esel},code=~"5.."}}[15s]))'
    return {
        "rps": total,
        "error_ratio": f"(({errors}) or vector(0)) / clamp_min({total}, 0.001)",
        "memory": f"max by (pod)(container_memory_working_set_bytes{{{pod}}})",
        "cpu": f"sum by (pod)(rate(container_cpu_usage_seconds_total{{{pod}}}[30s]))",
    }


def _first(points, pred, after=None):
    for t, v in points:
        if (after is None or t >= after) and pred(v):
            return t
    return None


def metric_facts(store: EvidenceStore, prom, ns: str, incident: dict, start: float, end: float,
                 workloads: list[dict], pods: list[dict], entry: str) -> None:
    q = _q(ns, entry)
    ref = incident.get("detected_at") or end

    rps = (prom.query_range(q["rps"], start, end, 5) or [{"values": []}])[0]["values"]
    pre = [v for t, v in rps if t < ref - 20] or [v for _, v in rps[: max(3, len(rps) // 4)]]
    if rps and pre:
        base = median(pre)
        peak_t, peak = max(rps, key=lambda p: p[1])
        if base > 0 and peak >= max(1.5 * base, base + 10):
            onset = _first(rps, lambda v: v >= base + 0.3 * (peak - base))
            store.add("prometheus", f"service/{entry}", "metric_traffic_change",
                      f"Request rate at {entry} rose from ~{base:.0f} req/s to a peak of {peak:.0f} req/s "
                      f"({peak / base:.1f}x) starting around this time", t=onset, baseline=base, peak=peak,
                      peak_t=peak_t, query=q["rps"])
        else:
            store.add("prometheus", f"service/{entry}", "metric_traffic_steady",
                      f"Request rate at {entry} stayed near ~{base:.0f} req/s (peak {peak:.0f})",
                      baseline=base, peak=peak, query=q["rps"])

    err = (prom.query_range(q["error_ratio"], start, end, 5) or [{"values": []}])[0]["values"]
    if err:
        peak_t, peak = max(err, key=lambda p: p[1])
        onset = _first(err, lambda v: v >= 0.05)
        store.add("prometheus", f"service/{entry}", "metric_error_ratio",
                  f"HTTP 5xx ratio at {entry} peaked at {peak:.0%}"
                  + (f"; first exceeded 5% at this time" if onset else ""), t=onset, peak=peak, peak_t=peak_t,
                  query=q["error_ratio"])

    limits = {p["name"]: sum(c["memory_limit_bytes"] or 0 for c in p["containers"]) or None for p in pods}
    owner = {p["name"]: next((w["name"] for w in workloads
                              if w["selector"] and all(p["labels"].get(k) == v for k, v in w["selector"].items())), None)
             for p in pods}
    for s in prom.query_range(q["memory"], start, end, 5) or []:
        pod = s["labels"].get("pod")
        if pod not in owner or not limits.get(pod) or not s["values"]:
            continue
        ratio = [(t, v / limits[pod]) for t, v in s["values"]]
        peak_t, peak = max(ratio, key=lambda p: p[1])
        if peak >= 0.8:
            store.add("prometheus", f"workload/{owner[pod]}", "metric_memory_high",
                      f"Pod {pod} memory working set reached {peak:.0%} of its limit",
                      t=_first(ratio, lambda v: v >= 0.8), pod=pod, peak_ratio=peak, query=q["memory"])
