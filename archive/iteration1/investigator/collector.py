"""Evidence collection: pulls Kubernetes state, events, logs and metrics for an incident window."""
import json
import time

from .kube import Kube, read_journal, ts
from .prom import Prometheus, PrometheusError


def metric_queries(ns: str, entry: str) -> dict[str, str]:
    sel = f'namespace="{ns}"'
    entry_sel = f'{sel},app="{entry}"'
    # Pod-level cgroup series (container=""): cAdvisor always exposes these, whereas per-container
    # series depend on the runtime (absent on minikube's docker driver). Limits are summed per pod.
    ctr = f'{sel},container="",pod!=""'
    total = f'sum(rate(http_requests_total{{{entry_sel}}}[15s]))'
    errors = f'sum(rate(http_requests_total{{{entry_sel},code=~"5.."}}[15s]))'
    return {
        "entry_rps": total,
        "entry_5xx_rps": f"({errors}) or vector(0)",
        "entry_error_ratio": f"(({errors}) or vector(0)) / clamp_min({total}, 0.001)",
        "entry_5xx_by_code": f'sum by (code)(rate(http_requests_total{{{entry_sel},code=~"5.."}}[15s]))',
        "entry_p95_latency_s": f"histogram_quantile(0.95, sum by (le)(rate(http_request_duration_seconds_bucket{{{entry_sel}}}[15s])))",
        "upstream_errors_by_reason": f"sum by (upstream, reason)(rate(upstream_errors_total{{{sel}}}[15s]))",
        "app_rps_by_pod": f"sum by (app, pod)(rate(http_requests_total{{{sel}}}[15s]))",
        "app_inflight_by_pod": f"max by (app, pod)(backend_inflight_requests{{{sel}}})",
        "cpu_cores_by_pod": f"sum by (pod)(rate(container_cpu_usage_seconds_total{{{ctr}}}[30s]))",
        "cpu_throttle_ratio_by_pod": (
            f"sum by (pod)(rate(container_cpu_cfs_throttled_periods_total{{{ctr}}}[30s]))"
            f" / clamp_min(sum by (pod)(rate(container_cpu_cfs_periods_total{{{ctr}}}[30s])), 0.001)"
        ),
        "memory_working_set_by_pod": f"max by (pod)(container_memory_working_set_bytes{{{ctr}}})",
        "oom_events_by_pod": f"sum by (pod)(increase(container_oom_events_total{{{ctr}}}[1m]))",
        "process_start_time_by_pod": f"max by (app, pod)(process_start_time_seconds{{{sel}}})",
        "scrape_up_by_pod": f'max by (app, pod)(up{{job="kubernetes-pods",{sel}}})',
    }


def parse_log_line(line: str) -> dict:
    try:
        rec = json.loads(line)
        if isinstance(rec, dict):
            rec["t"] = ts(rec.get("ts")) if rec.get("ts") else None
            return rec
    except json.JSONDecodeError:
        pass
    return {"t": None, "level": "unknown", "msg": line[:500]}


def collect(settings, prom: Prometheus, kube: Kube, incident: dict, start: float, end: float,
            journal_path=None, log=print) -> dict:
    ns = settings.namespace
    sources = []

    def source(name, fn):
        t0 = time.time()
        try:
            result = fn()
            sources.append({"source": name, "status": "ok", "ms": int((time.time() - t0) * 1000)})
            return result
        except Exception as exc:  # noqa: BLE001 - one failed source must not abort the investigation
            sources.append({"source": name, "status": "error", "error": str(exc)[:300]})
            log(f"  ! evidence source {name} failed: {exc}")
            return None

    log(f"  collecting Kubernetes state for namespace '{ns}'")
    k8s = {
        "namespace": source("k8s.namespace", lambda: kube.namespace(ns)),
        "deployments": source("k8s.deployments", lambda: kube.deployments(ns)) or [],
        "replicasets": source("k8s.replicasets", lambda: kube.replicasets(ns)) or [],
        "services": source("k8s.services", lambda: kube.services(ns)) or [],
        "configmaps": source("k8s.configmaps", lambda: kube.configmaps(ns)) or [],
        "pods": source("k8s.pods", lambda: kube.pods(ns)) or [],
        "events": [e for e in (source("k8s.events", lambda: kube.events(ns)) or [])
                   if (e["last"] or e["first"] or 0) >= start - 60 and (e["first"] or 0) <= end + 60],
        "node_events": [e for e in (source("k8s.node_events", kube.node_events) or [])
                        if (e["last"] or e["first"] or 0) >= start - 60],
    }

    journal = read_journal(journal_path, start - 60, end + 60) if journal_path else []
    sources.append({"source": "k8s.pod_journal", "status": "ok" if journal_path else "unavailable",
                    "entries": len(journal)})

    log(f"  querying Prometheus ({int(end - start)}s window)")
    metrics = {}
    for name, q in metric_queries(ns, settings.entry_app).items():
        series = source(f"prometheus.{name}", lambda q=q: prom.query_range(q, start, end, step=5))
        metrics[name] = {"query": q, "series": series or []}

    log("  fetching container logs (current + previous instances)")
    logs = {}
    since = int(prom.now() - start) + 30
    for pod in k8s["pods"]:
        for c in pod["containers"]:
            key = f"{pod['name']}/{c['name']}"
            current = source(f"logs.{key}", lambda p=pod, c=c: kube.logs(ns, p["name"], c["name"], since_s=since))
            previous = []
            if c["restart_count"] > 0:
                previous = source(f"logs.{key}.previous",
                                  lambda p=pod, c=c: kube.logs(ns, p["name"], c["name"], previous=True, tail=400)) or []
            recs = []
            for instance, lines in (("previous", previous), ("current", current or [])):
                for line in lines:
                    r = parse_log_line(line)
                    r["instance"] = instance
                    r["pod"], r["container"] = pod["name"], c["name"]
                    if r["t"] is None or start - 5 <= r["t"] <= end + 5:
                        recs.append(r)
            logs[key] = recs

    return {
        "incident": incident,
        "window": {"start": start, "end": end},
        "collected_at": prom.now(),
        "k8s": k8s,
        "journal": journal,
        "metrics": metrics,
        "logs": logs,
        "sources": sources,
    }


def check_metrics_available(prom: Prometheus, settings) -> dict:
    """Quick sanity check used by `status`/`check` commands."""
    out = {}
    for name in ("entry_rps", "cpu_cores_by_pod", "memory_working_set_by_pod"):
        try:
            out[name] = len(prom.query(metric_queries(settings.namespace, settings.entry_app)[name]))
        except PrometheusError as exc:
            out[name] = f"error: {exc}"
    return out
