"""Prometheus adapter: implements get_metrics() with PromQL. Optional; investigations work without it."""
from .base import MetricSeries, MetricsProvider, TimeRange


class PrometheusMetrics(MetricsProvider):
    name = "prometheus"

    def __init__(self, prom, namespace: str):
        self.prom, self.ns = prom, namespace

    def _query(self, metric: str, target: str | None) -> str:
        sel = f'namespace="{self.ns}"'
        pod = f'{sel},container="",pod!=""'  # pod-level cgroup series: always exported by cAdvisor
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

    def get_metrics(self, metric: str, time_range: TimeRange, target: str | None = None) -> list[MetricSeries]:
        q = self._query(metric, target)
        out = []
        for s in self.prom.query_range(q, time_range.start, time_range.end, 5) or []:
            labels = {"query": q}
            if s["labels"].get("pod"):
                labels["instance"] = s["labels"]["pod"]
            out.append(MetricSeries(labels=labels, points=s["values"]))
        return out
