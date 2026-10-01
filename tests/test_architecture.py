"""Architecture guards: the investigation engine must stay provider-independent (docs/ARCHITECTURE.md §2-3).

Engine modules may only obtain evidence through `investigator.capabilities` (the interface and records).
Provider adapters (capabilities/kubernetes.py, capabilities/prometheus.py) and provider clients
(kube.py, prom.py) must not be imported by them.
"""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "investigator"
ENGINE_MODULES = ["collect.py", "dependencies.py", "diagnosis.py", "metrics.py", "logparse.py", "evidence.py",
                  "report.py", "planner.py", "context.py", "detector.py", "pipeline.py", "__main__.py", "timeline.py", "impact.py", "remediation.py",
                  "remediation_model.py"]
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
                     "context.py", "report.py", "timeline.py", "impact.py", "remediation.py", "remediation_model.py"]
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


def test_remediation_planning_has_no_path_to_the_system():
    """Iteration 5 boundary: the planner and its contract cannot reach a provider, a shell, the network or Slack,
    and plan_remediation takes no capabilities object."""
    import inspect

    from investigator.remediation import plan_remediation
    banned = ("capabilities", "kube", "kubernetes", "prom", "providers", "subprocess", "os", "shutil", "socket",
              "requests", "urllib", "http", "slack", "sqlite3", "history_store", "dependencies", "collect", "planner")
    for name in ("remediation.py", "remediation_model.py"):
        bad = {i for i in _imports(PKG / name) for b in banned
               if i == b or i.startswith(b + ".") or i.endswith("." + b) or i.split(".")[0] == b}
        assert not bad, (name, bad)
    assert list(inspect.signature(plan_remediation).parameters) == ["dx", "reconstruction", "impact", "store",
                                                                    "incident_id"]
    assert not _imports(PKG / "remediation_model.py") - {"dataclasses", "dataclasses.asdict", "dataclasses.dataclass",
                                                         "dataclasses.field", "enum", "enum.Enum"}


def test_human_review_layers_keep_their_boundaries():
    """Iteration 6: the review model is independent of Slack and has no path to the system; the Slack layer cannot
    reach infrastructure or remediation logic; the planner does not know about Slack or the review."""
    no_system = ("capabilities", "kube", "kubernetes", "prom", "providers", "subprocess", "shutil", "socket",
                 "history_store", "collect", "planner", "dependencies", "diagnosis", "remediation")

    def offenders(name, banned):
        return {i for i in _imports(PKG / name) for b in banned
                if i == b or i.startswith(b + ".") or i.endswith("." + b) or i.split(".")[0] == b}
    assert not offenders("review.py", no_system + ("slack", "slack_view", "slack_app", "slack_sdk", "requests", "os",
                                                   "urllib", "http"))
    for name in ("slack.py", "slack_view.py", "slack_app.py"):
        assert not offenders(name, no_system), name
    assert not offenders("remediation.py", ("slack", "slack_view", "slack_app", "slack_sdk", "review"))
    # presentation only: slack_view makes no remediation or review decisions
    src = (PKG / "slack_view.py").read_text(encoding="utf-8")
    assert "plan_remediation" not in src and ".decide(" not in src


def test_adapters_implement_the_whole_interface():
    from investigator.capabilities.base import MetricsProvider, ResourceProvider
    from investigator.capabilities.kubernetes import KubernetesAdapter
    from investigator.capabilities.prometheus import PrometheusMetrics
    assert not getattr(KubernetesAdapter, "__abstractmethods__", None)
    assert not getattr(PrometheusMetrics, "__abstractmethods__", None)
    assert issubclass(KubernetesAdapter, ResourceProvider) and issubclass(PrometheusMetrics, MetricsProvider)
