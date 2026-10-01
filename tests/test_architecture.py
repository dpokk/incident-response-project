"""Architecture guards: the investigation engine must stay provider-independent (docs/ARCHITECTURE.md §2-3).

Engine modules may only obtain evidence through `investigator.capabilities` (the interface and records).
Provider adapters (capabilities/kubernetes.py, capabilities/prometheus.py) and provider clients
(kube.py, prom.py) must not be imported by them.
"""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "investigator"
ENGINE_MODULES = ["collect.py", "dependencies.py", "diagnosis.py", "metrics.py", "logparse.py", "evidence.py",
                  "report.py", "planner.py", "context.py", "detector.py", "pipeline.py", "__main__.py", "timeline.py"]
# providers.py is the composition root: the one place allowed to choose concrete adapters.
# The evidence history store and recorder are reached only through capabilities (Iteration 4).
FORBIDDEN = ("kubernetes", "kube", "prom", "capabilities.kubernetes", "capabilities.prometheus", "requests",
             "history_store", "capabilities.kubernetes_recorder", "kubernetes_recorder", "sqlite3")


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


REASONING_MODULES = ["collect.py", "dependencies.py", "diagnosis.py", "planner.py", "detector.py", "metrics.py",
                     "context.py", "report.py", "timeline.py"]
PROVIDER_WORDS = ("OOMKilled", "CrashLoopBackOff", "BackOff", "ImagePull", "ErrImagePull", "CreateContainer",
                  "FailedScheduling", "ScalingReplicaSet", "Unhealthy", "Killing", "kubelet", "ReplicaSet",
                  "workload/", "k8s_event", "container_terminated")


def _code_strings(path: Path) -> list[str]:
    """String and number literals used by the code (docstrings and comments excluded)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    return [str(n.value) for n in ast.walk(tree) if isinstance(n, ast.Constant) and id(n) not in docstrings]


def test_reasoning_uses_neutral_vocabulary():
    """Diagnosis, planning and detection reason over neutral causes/categories, never Kubernetes reason strings."""
    offenders = {}
    for name in REASONING_MODULES:
        found = sorted({w for s in _code_strings(PKG / name) for w in PROVIDER_WORDS if w in s}
                       | {s for s in _code_strings(PKG / name) if s in ("137", "143")})
        if found:
            offenders[name] = found
    assert not offenders, f"provider-specific vocabulary in reasoning code: {offenders}"


def test_history_store_is_provider_neutral():
    """The store holds evidence for any provider: it must not depend on Kubernetes or its vocabulary."""
    path = PKG / "history_store.py"
    bad = {i for i in _imports(path) for f in ("kubernetes", "kube", "prom", "capabilities") if i == f or i.startswith(f + ".")
           or i.endswith("." + f)}
    words = {w for s in _code_strings(path) for w in PROVIDER_WORDS + ("Pod", "ConfigMap", "namespace") if w in s}
    assert not bad and not words, (bad, words)


def test_adapters_implement_the_whole_interface():
    from investigator.capabilities.base import MetricsProvider, ResourceProvider
    from investigator.capabilities.kubernetes import KubernetesAdapter
    from investigator.capabilities.prometheus import PrometheusMetrics
    assert not getattr(KubernetesAdapter, "__abstractmethods__", None)
    assert not getattr(PrometheusMetrics, "__abstractmethods__", None)
    assert issubclass(KubernetesAdapter, ResourceProvider) and issubclass(PrometheusMetrics, MetricsProvider)
