"""Metrics as time-anchored evidence (Iteration 4), through the `get_metrics` capability. Optional: the
investigation works without a metrics provider, and the planner only asks for metrics when there is a reason.

Every metric fact says what was measured and *when a threshold was crossed*. A crossing happened somewhere
between the last sample on one side and the first sample on the other, so its time is reported as that
bounded interval, never as a point. The query reaches back before the window so the baseline - the "before
incident" state - is measured, not assumed.

Facts (all provider-neutral):
  metric_traffic_change      request rate rose clearly above its baseline (onset: bounded)
  metric_traffic_steady      it did not
  metric_error_ratio         user-facing error ratio exceeded 5% (onset: bounded)
  metric_error_ratio_recovered   ... and fell back below it after the peak (bounded)
  metric_memory_high         an instance's memory reached >= 80% of its limit (crossing: bounded)
  metric_memory_observed     what memory sampling actually saw for a component: peak, how many samples,
                             including instances that no longer exist - so a peak that was never sampled is a
                             stated sampling limit, not evidence against memory exhaustion
"""
from .capabilities import Capabilities, TimeRange
from .capabilities.base import ResourceState
from .evidence import EvidenceStore

BASELINE_S = 600          # how far before the window the metrics query reaches, to measure the baseline
ERROR_THRESHOLD = 0.05
MEMORY_HIGH = 0.8
SPARSE_SPACING_S = 15     # samples further apart than this can miss a short-lived peak


def crossing(points: list, pred, sustained: int = 1) -> tuple | None:
    """First crossing into `pred`: (last sample before, first sample in), each (t, value). The crossing happened
    in between. (None, sample) if the series already satisfied `pred` at its first sample."""
    for i, (t, v) in enumerate(points):
        if pred(v) and all(pred(x) for _, x in points[i:i + sustained]):
            return (points[i - 1] if i > 0 else None, (t, v))
    return None


def episodes(points: list, pred, sustained: int = 1) -> list[tuple]:
    """Every stretch where `pred` holds: [(crossing in, crossing out or None)], crossings as in `crossing`."""
    out, i = [], 0
    while i < len(points):
        rest = points[i:]
        up = crossing(rest, pred, sustained)
        if up is None:
            break
        j = i + rest.index(up[1])
        down = crossing(points[j:], lambda v: not pred(v), sustained)
        out.append((up, down))
        if down is None:
            break
        i = j + points[j:].index(down[1])
    return out


def episode_for(eps: list, tr: TimeRange) -> tuple:
    """The episode that overlaps the investigated window (the earliest such), and the earlier ones."""
    def end_t(ep):
        return ep[1][1][0] if ep[1] else float("inf")
    overlapping = [ep for ep in eps if ep[0][1][0] <= tr.end and end_t(ep) >= tr.start]
    chosen = overlapping[0] if overlapping else None
    earlier = [ep for ep in eps if end_t(ep) < tr.start]
    return chosen, earlier


def _when(cross: tuple, window_s: float = 0) -> dict:
    """The crossing happened after the last sample before it - minus the averaging window of a rate, whose points
    lag the change - and no later than the first sample after it."""
    before, (t, _) = cross
    if before is None:
        return {"t": t, "t_basis": "observed"}                       # already true at the first sample
    return {"t": t, "t_basis": "bounded", "t_earliest": before[0] - window_s, "t_latest": t}


def _hms(t) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(t).astimezone().strftime("%H:%M:%S") if t else "?"


def metric_facts(store: EvidenceStore, caps: Capabilities, incident: dict, tr: TimeRange,
                 states: list[ResourceState], entry: str | None, components: list[str] | None = None) -> None:
    src = caps.metrics.name
    wide = TimeRange(tr.start - BASELINE_S, tr.end)
    if entry:
        _traffic(store, caps, src, entry, wide, tr)
        _errors(store, caps, src, entry, wide, tr)
    near = TimeRange(tr.start - 60, tr.end)        # memory: the instances of this incident, not earlier episodes
    for rs in states:
        if components is None or rs.component in components:
            _memory(store, caps, src, rs, near)


def _earlier(eps) -> str:
    if not eps:
        return ""
    return (f"; {len(eps)} earlier episode(s) before the window (from {_hms(eps[0][0][1][0])}), not counted as this "
            f"incident's")


def _traffic(store, caps, src, entry, wide, tr) -> None:
    series = caps.get_metrics("request_rate", wide, entry) or []
    rps = series[0].points if series else []
    if len(rps) < 3:
        return
    values = sorted(v for _, v in rps)
    base = values[len(values) // 4]          # lower quartile: the usual rate, robust to spikes in the lookback
    q = series[0].labels.get("query")
    in_window = [p for p in rps if tr.start <= p[0] <= tr.end] or rps
    peak = max(v for _, v in in_window)
    if base > 0 and peak >= max(1.5 * base, base + 10):
        level = base + 0.3 * (peak - base)
        chosen, earlier = episode_for(episodes(rps, lambda v: v >= level, sustained=2), tr)
        if chosen:
            seg = [p for p in rps if p[0] >= chosen[0][1][0] and (chosen[1] is None or p[0] <= chosen[1][1][0])]
            peak_t, peak = max(seg, key=lambda p: p[1])
            when = _when(chosen[0], series[0].labels.get("window_s") or 0)
            span = (f"between {_hms(when['t_earliest'])} and {_hms(when['t_latest'])}" if when["t_basis"] == "bounded"
                    else f"already by {_hms(when['t'])} (before the measured range)")
            store.add(src, f"service/{entry}", "metric_traffic_change",
                      f"Request rate at {entry} rose from its usual ~{base:.0f} req/s to a peak of {peak:.0f} req/s "
                      f"({peak / base:.1f}x); the rise began {span}" + _earlier(earlier),
                      **when, baseline=base, peak=peak, peak_t=peak_t, component=entry, query=q,
                      earlier_episodes=len(earlier))
            return
    store.add(src, f"service/{entry}", "metric_traffic_steady",
              f"Request rate at {entry} stayed near its usual ~{base:.0f} req/s in the window (peak {peak:.0f})",
              t_basis="unknown", baseline=base, peak=peak, component=entry, query=q)


def _errors(store, caps, src, entry, wide, tr) -> None:
    series = caps.get_metrics("error_ratio", wide, entry) or []
    err = series[0].points if series else []
    if not err:
        return
    q = series[0].labels.get("query")
    chosen, earlier = episode_for(episodes(err, lambda v: v >= ERROR_THRESHOLD, sustained=2), tr)
    if chosen is None:
        return
    up, down = chosen
    seg = [p for p in err if p[0] >= up[1][0] and (down is None or p[0] <= down[1][0])]
    peak_t, peak = max(seg, key=lambda p: p[1])
    store.add(src, f"service/{entry}", "metric_error_ratio",
              f"HTTP 5xx ratio at {entry} exceeded {ERROR_THRESHOLD:.0%} (peak {peak:.0%} at {_hms(peak_t)})"
              + _earlier(earlier), **_when(up, series[0].labels.get("window_s") or 0), peak=peak, peak_t=peak_t, component=entry, query=q,
              earlier_episodes=len(earlier))
    if down is not None and down[0] is not None:
        store.add(src, f"service/{entry}", "metric_error_ratio_recovered",
                  f"HTTP 5xx ratio at {entry} fell back below {ERROR_THRESHOLD:.0%} after its peak",
                  **_when(down, series[0].labels.get("window_s") or 0), component=entry, query=q)


def _memory(store, caps, src, rs: ResourceState, wide) -> None:
    limit = next((p.memory_limit_bytes for i in rs.instances for p in i.processes if p.memory_limit_bytes), None) \
        or next((h.memory_limit_bytes for h in rs.history if h.memory_limit_bytes), None)
    series = [s for s in caps.get_metrics("memory_working_set", wide, component=rs.component) or [] if s.points]
    if not series or not limit:
        return
    current = {i.name for i in rs.instances}
    peaks, samples, high = [], [], []
    for s in series:
        inst = s.labels.get("instance")
        ratio = [(t, v / limit) for t, v in s.points]
        peak_t, peak = max(ratio, key=lambda p: p[1])
        peaks.append((peak, inst, peak_t))
        n = s.labels.get("samples")
        if isinstance(n, int):
            samples.append((n, ratio[-1][0] - ratio[0][0]))
        cross = crossing(ratio, lambda v: v >= MEMORY_HIGH)
        if cross:
            high.append(store.add(
                src, f"component/{rs.component}", "metric_memory_high",
                f"Instance {inst} memory working set reached {MEMORY_HIGH:.0%} of its limit (peak {peak:.0%})"
                + ("; that instance no longer exists" if inst not in current else ""),
                **_when(cross), instance=inst, peak_ratio=peak, instance_gone=inst not in current,
                query=s.labels.get("query")))
    top, top_inst, top_t = max(peaks, key=lambda p: p[0])
    total = sum(n for n, _ in samples)
    span = sum(d for _, d in samples)
    spacing = (span / total) if total else None
    gone = sum(1 for s in series if s.labels.get("instance") not in current)
    sparse = spacing is not None and spacing > SPARSE_SPACING_S
    store.add(src, f"component/{rs.component}", "metric_memory_observed",
              f"Memory of {len(series)} {rs.component} instance(s)" + (f" ({gone} no longer existing)" if gone else "")
              + f" was sampled; the highest value seen was {top:.0%} of the limit ({top_inst})"
              + (f"; about one sample every {spacing:.0f}s" if spacing else "")
              + ("; a short-lived peak between samples would not have been seen" if sparse else ""),
              t_basis="unknown", peak_ratio=top, peak_instance=top_inst, peak_t=top_t, instances=len(series),
              instances_gone=gone, samples=total or None, spacing_s=spacing, sparse=sparse,
              crossed_high=bool(high))
