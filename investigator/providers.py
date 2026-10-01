"""Composition root: connects the configured providers. The only module outside capabilities/ that knows
which concrete providers exist (today: Kubernetes for resources, Prometheus for metrics).

Everything else (detector, planner, evidence recording, diagnosis, report) depends only on the capability
interface in capabilities/base.py. Adding a second provider means adding an adapter and choosing it here.
"""
import time
from dataclasses import dataclass
from typing import Callable

from .capabilities import Capabilities
from .capabilities.base import MetricsProvider, ResourceProvider
from .evidence import EvidenceStore


@dataclass
class Providers:
    new_resources: Callable[[], ResourceProvider]   # a fresh adapter (clean caches) per investigation
    metrics: MetricsProvider | None
    clock: Callable[[], float]                       # the observed system's clock (handles VM clock skew)
    clock_offset: float = 0.0
    history: object | None = None                    # the evidence history store, if enabled

    def capabilities(self, store: EvidenceStore) -> Capabilities:
        return Capabilities(self.new_resources(), store, self.metrics)

    def describe(self) -> str:
        r = self.new_resources()
        return (f"{r.name} ({r.scope})" + (f" + {self.metrics.name}" if self.metrics else "")
                + (" with evidence history" if self.history is not None else ""))


def connect(settings, log=print) -> Providers:
    from .capabilities.kubernetes import KubernetesAdapter
    from .kube import Kube

    kube = Kube(settings.kube_context)
    metrics, clock, offset = None, time.time, 0.0
    if settings.prometheus_enabled:
        try:
            from .capabilities.prometheus import PrometheusMetrics
            from .prom import connect as connect_prom
            prom = connect_prom(settings)
            metrics, clock, offset = PrometheusMetrics(prom, settings.namespace), prom.now, prom.clock_offset
        except Exception as exc:  # noqa: BLE001 - metrics are optional
            log(f"metrics source unavailable ({exc}); continuing without metrics")

    history = None
    if settings.history_enabled:
        from .history_store import HistoryStore
        history = HistoryStore(settings.history_path, clock=clock,
                               max_lines_per_minute=settings.history_max_lines_per_min)
    recorder_options = {"retention_s": settings.history_retention_h * 3600, "poll_s": settings.poll_interval_s,
                        "log": log}

    def new_resources() -> ResourceProvider:
        return KubernetesAdapter(kube, settings.namespace, journal_path=settings.state_dir / "pod_journal.jsonl",
                                 active_probes=settings.active_probes, clock=clock, history=history,
                                 recorder_options=recorder_options)
    return Providers(new_resources, metrics, clock, offset, history)
