"""Architecture guards: the investigation engine must stay provider-independent (docs/ARCHITECTURE.md §2-3).

Engine modules may only obtain evidence through `investigator.capabilities` (the interface and records).
Provider adapters (capabilities/kubernetes.py, capabilities/prometheus.py) and provider clients
(kube.py, prom.py) must not be imported by them.
"""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "investigator"
ENGINE_MODULES = ["collect.py", "dependencies.py", "diagnosis.py", "metrics.py", "logparse.py", "evidence.py",
                  "report.py", "planner.py", "context.py"]
FORBIDDEN = ("kubernetes", "kube", "prom", "capabilities.kubernetes", "capabilities.prometheus", "requests")


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names.add(mod)
            names |= {f"{mod}.{a.name}" if mod else a.name for a in node.names}
    return names


def test_engine_does_not_import_providers():
    offenders = {}
    for name in ENGINE_MODULES:
        path = PKG / name
        if not path.exists():
            continue
        bad = {i for i in _imports(path) for f in FORBIDDEN if i == f or i.startswith(f + ".") or i.endswith("." + f)}
        if bad:
            offenders[name] = sorted(bad)
    assert not offenders, f"engine modules import provider code: {offenders}"


def test_adapters_implement_the_whole_interface():
    from investigator.capabilities.base import MetricsProvider, ResourceProvider
    from investigator.capabilities.kubernetes import KubernetesAdapter
    from investigator.capabilities.prometheus import PrometheusMetrics
    assert not getattr(KubernetesAdapter, "__abstractmethods__", None)
    assert not getattr(PrometheusMetrics, "__abstractmethods__", None)
    assert issubclass(KubernetesAdapter, ResourceProvider) and issubclass(PrometheusMetrics, MetricsProvider)
