"""Optional metrics enrichment through the `get_metrics` capability. The investigation works without it."""
from statistics import median

from .capabilities import Capabilities, TimeRange
from .capabilities.base import ResourceState
from .evidence import EvidenceStore


def _first(points, pred, after=None):
    for t, v in points:
        if (after is None or t >= after) and pred(v):
            return t
    return None


def metric_facts(store: EvidenceStore, caps: Capabilities, incident: dict, tr: TimeRange,
                 states: list[ResourceState], entry: str) -> None:
    src = caps.metrics.name
    ref = incident.get("detected_at") or tr.end

    series = caps.get_metrics("request_rate", tr, entry) or []
    rps = series[0].points if series else []
    pre = [v for t, v in rps if t < ref - 20] or [v for _, v in rps[: max(3, len(rps) // 4)]]
    if rps and pre:
        base = median(pre)
        peak_t, peak = max(rps, key=lambda p: p[1])
        if base > 0 and peak >= max(1.5 * base, base + 10):
            onset = _first(rps, lambda v: v >= base + 0.3 * (peak - base))
            store.add(src, f"service/{entry}", "metric_traffic_change",
                      f"Request rate at {entry} rose from ~{base:.0f} req/s to a peak of {peak:.0f} req/s "
                      f"({peak / base:.1f}x) starting around this time", t=onset, baseline=base, peak=peak,
                      peak_t=peak_t, query=series[0].labels.get("query"))
        else:
            store.add(src, f"service/{entry}", "metric_traffic_steady",
                      f"Request rate at {entry} stayed near ~{base:.0f} req/s (peak {peak:.0f})",
                      baseline=base, peak=peak, query=series[0].labels.get("query"))

    series = caps.get_metrics("error_ratio", tr, entry) or []
    err = series[0].points if series else []
    if err:
        peak_t, peak = max(err, key=lambda p: p[1])
        onset = _first(err, lambda v: v >= 0.05)
        store.add(src, f"service/{entry}", "metric_error_ratio",
                  f"HTTP 5xx ratio at {entry} peaked at {peak:.0%}"
                  + ("; first exceeded 5% at this time" if onset else ""), t=onset, peak=peak, peak_t=peak_t,
                  query=series[0].labels.get("query"))

    limits, owner = {}, {}
    for rs in states:
        for inst in rs.instances:
            limits[inst.name] = sum(p.memory_limit_bytes or 0 for p in inst.processes) or None
            owner[inst.name] = rs.component
    for s in caps.get_metrics("memory_working_set", tr) or []:
        inst = s.labels.get("instance")
        if inst not in owner or not limits.get(inst) or not s.points:
            continue
        ratio = [(t, v / limits[inst]) for t, v in s.points]
        peak_t, peak = max(ratio, key=lambda p: p[1])
        if peak >= 0.8:
            store.add(src, f"workload/{owner[inst]}", "metric_memory_high",
                      f"Pod {inst} memory working set reached {peak:.0%} of its limit",
                      t=_first(ratio, lambda v: v >= 0.8), pod=inst, peak_ratio=peak, query=s.labels.get("query"))
