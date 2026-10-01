"""Prometheus adapter: implements get_metrics() with PromQL. Optional; investigations work without it."""
from .base import MetricSeries, MetricsProvider, TimeRange

RATE_WINDOWS = {"request_rate": 15, "error_ratio": 15, "cpu_usage": 30}   # the [..s] windows used below


class PrometheusMetrics(MetricsProvider):
    name = "prometheus"

    def __init__(self, prom, namespace: str):
        self.prom, self.ns = prom, namespace

    def _query(self, metric: str, target: str | None, component: str | None = None) -> str:
        sel = f'namespace="{self.ns}"'
        # Pod-level cgroup series (always exported by cAdvisor). A component's pods - including deleted ones,
        # whose series Prometheus keeps - are those named "<component>-...".
        pod = f'{sel},container="",' + (f'pod=~"{component}-.+"' if component else 'pod!=""')
        esel = f'{sel},app="{target}"'
        total = f"sum(rate(http_requests_total{{{esel}}}[15s]))"
        errors = f'sum(rate(http_requests_total{{{esel},code=~"5.."}}[15s]))'
        queries = {
            "request_rate": total,
            "error_ratio": f"(({errors}) or vector(0)) / clamp_min({total}, 0.001)",
            "memory_working_set": f"max by (pod)(container_memory_working_set_bytes{{{pod}}})",
            "cpu_usage": f"sum by (pod)(rate(container_cpu_usage_seconds_total{{{pod}}}[30s]))",
        }
        if metric not in queries:
            raise ValueError(f"unknown metric {metric!r}")
        return queries[metric]

    def get_metrics(self, metric: str, time_range: TimeRange, target: str | None = None,
                    component: str | None = None) -> list[MetricSeries]:
        q = self._query(metric, target, component)
        out = []
        for s in self.prom.query_range(q, time_range.start, time_range.end, 5) or []:
            labels = {"query": q}
            if metric in RATE_WINDOWS:
                # Each point averages over this many seconds before it: a change shows up to that much late.
                labels["window_s"] = RATE_WINDOWS[metric]
            if s["labels"].get("pod"):
                labels["instance"] = s["labels"]["pod"]
                if component:
                    labels["component"] = component
            if metric in ("memory_working_set", "cpu_usage") and component:
                # The range query's 5 s step repeats the last sample between scrapes; count the real samples so the
                # report can say how densely memory was actually measured.
                labels["samples"] = self._sample_count(metric, s["labels"].get("pod"), time_range)
            out.append(MetricSeries(labels=labels, points=s["values"]))
        return out

    def _sample_count(self, metric: str, pod: str | None, time_range: TimeRange) -> int | None:
        if not pod:
            return None
        name = {"memory_working_set": "container_memory_working_set_bytes",
                "cpu_usage": "container_cpu_usage_seconds_total"}[metric]
        window = max(1, int(time_range.end - time_range.start))
        try:
            res = self.prom.query(f'count_over_time({name}{{namespace="{self.ns}",container="",pod="{pod}"}}[{window}s])',
                                  at=time_range.end)
            return int(res[0]["value"]) if res else 0
        except Exception:  # noqa: BLE001 - sample counts are an annotation, not evidence
            return None
