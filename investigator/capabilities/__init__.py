"""Capability layer: the only way the investigation engine obtains evidence.

`Capabilities` wraps a ResourceProvider (and optionally a MetricsProvider), records every call in the
investigation trace (capability, provider, arguments, result size, duration), and caches results for
the lifetime of one investigation so repeated questions are cheap and consistent.
"""
import time

from ..evidence import EvidenceStore
from .base import MetricsProvider, ResourceProvider, TimeRange

__all__ = ["Capabilities", "TimeRange"]


class Capabilities:
    RESOURCE_CAPABILITIES = ("list_components", "get_resource_state", "get_events", "get_logs", "get_configuration",
                             "get_dependencies", "list_services", "get_service_health", "check_connectivity",
                             "probe_request", "get_deployment_history",
                             # evidence history (Iteration 4)
                             "get_log_history", "get_configuration_history", "get_evidence_coverage",
                             "get_availability_history")

    def __init__(self, resources: ResourceProvider, store: EvidenceStore, metrics: MetricsProvider | None = None):
        self.resources, self.metrics, self.store = resources, metrics, store
        self._cache: dict = {}

    @property
    def available(self) -> list[str]:
        return list(self.RESOURCE_CAPABILITIES) + (["get_metrics"] if self.metrics else [])

    def _call(self, provider, capability: str, *args, **kwargs):
        key = (capability, args, tuple(sorted(kwargs.items())))
        if key in self._cache:
            return self._cache[key]
        t0 = time.time()
        try:
            result, status = getattr(provider, capability)(*args, **kwargs), "ok"
        except Exception as exc:  # noqa: BLE001 - a failed capability is recorded, not fatal
            result, status = None, f"error: {type(exc).__name__}: {str(exc)[:200]}"
        self.store.step("capability", capability, provider=provider.name, args=[_short(a) for a in args],
                        kwargs={k: _short(v) for k, v in kwargs.items()}, status=status,
                        ms=int((time.time() - t0) * 1000), summary=_summarize(result))
        self._cache[key] = result
        return result

    def __getattr__(self, capability: str):
        if capability in self.RESOURCE_CAPABILITIES:
            return lambda *a, **k: self._call(self.resources, capability, *a, **k)
        if capability == "get_metrics":
            if self.metrics is None:
                return lambda *a, **k: None
            return lambda *a, **k: self._call(self.metrics, "get_metrics", *a, **k)
        raise AttributeError(capability)


def _short(v) -> str:
    if isinstance(v, TimeRange):
        return f"{time.strftime('%H:%M:%S', time.localtime(v.start))}-{time.strftime('%H:%M:%S', time.localtime(v.end))}"
    return str(v)


def _summarize(result) -> str:
    if result is None:
        return "none"
    if isinstance(result, list):
        return f"{len(result)} items"
    return type(result).__name__
