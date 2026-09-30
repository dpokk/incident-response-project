"""Offline tests: the same investigator, four different broken clusters, no scenario hints.

Run with:  python -m pytest tests -q     (or: python tests/test_scenarios.py)
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fake_cluster import FakeKube, NOW, base_world, world_crash, world_db_down, world_db_misconfig, world_oom  # noqa: E402

from investigator import collect as collect_mod  # noqa: E402
from investigator import diagnosis as diagnosis_mod  # noqa: E402
from investigator.collect import collect  # noqa: E402
from investigator.diagnosis import diagnose  # noqa: E402
from investigator.evidence import EvidenceStore  # noqa: E402
from investigator.report import build, render_text  # noqa: E402
from investigator.capabilities import Capabilities  # noqa: E402
from investigator.capabilities.kubernetes import KubernetesAdapter  # noqa: E402
from investigator.context import IncidentContext  # noqa: E402
from investigator.planner import plan_and_collect  # noqa: E402


USER_SYMPTOM = [{"kind": "entry_probe_failure", "subject": "component/frontend", "t": None,
                 "text": "Synthetic requests to frontend failing"}]


def run(world: dict, strategy: str = "planned", signals: list | None = None) -> tuple[dict, EvidenceStore]:
    """Investigate a fake cluster the same way the pipeline does. Only symptoms are passed in, never the scenario."""
    store = EvidenceStore()
    caps = Capabilities(KubernetesAdapter(FakeKube(world), "shop", journal_path=None, active_probes=True), store)
    sigs = [{**s, "t": s["t"] or NOW - 30} for s in (USER_SYMPTOM if signals is None else signals)]
    incident = {"id": "INC-TEST", "detected_at": NOW - 30, "namespace": "shop", "signals": sigs}
    entry = ("frontend", "8080", "/api/orders")
    if strategy == "exhaustive":
        collect(caps, incident, NOW - 330, NOW, entry=entry, log=lambda *_: None)
    else:
        ctx = IncidentContext.from_incident(incident, NOW - 330, NOW, entry=entry, metrics_target="frontend")
        plan_and_collect(caps, ctx, log=lambda *_: None)
    dx = diagnose(store)
    render_text(build(incident, store, dx, (NOW - 330, NOW), True))  # must render without errors
    return dx, store


def test_oom_is_memory_exhaustion():
    dx, _ = run(world_oom())
    assert dx["category"] == "memory_exhaustion"
    assert dx["affected_component"] == "backend"
    assert "frontend" in dx["impacted_components"]


def test_wrong_database_host_is_misconfiguration():
    dx, _ = run(world_db_misconfig())
    assert dx["category"] == "dependency_misconfiguration"
    assert dx["affected_component"] == "backend"
    assert dx["dependencies"][0]["host"] == "postgres-wrong"
    assert dx["dependencies"][0]["type"] == "PostgreSQL"


def test_database_down_is_dependency_unavailable():
    dx, _ = run(world_db_down())
    assert dx["category"] == "dependency_unavailable"
    assert dx["affected_component"] == "backend"
    assert dx["dependencies"][0]["host"] == "postgres"


def test_exception_exit_is_application_crash_not_oom():
    dx, _ = run(world_crash())
    assert dx["category"] == "application_crash"
    assert dx["affected_component"] == "backend"
    assert "DivisionByZero" in dx["root_cause"]
    rejected = {a["category"] for a in dx["alternatives"] if a["component"] == "backend"}
    assert "memory_exhaustion" in rejected


def test_root_cause_component_is_distinct_from_affected_component():
    """Scenario C: the failure surfaces in backend, but the cause is PostgreSQL (docs/INCIDENTS.md Incident 3)."""
    down, _ = run(world_db_down())
    assert down["affected_component"] == "backend"
    assert down["root_cause_component"]["name"] == "postgres" and down["root_cause_component"]["kind"] == "component"
    assert "frontend" in down["impacted_components"]
    wrong, _ = run(world_db_misconfig())
    assert wrong["root_cause_component"]["kind"] == "configuration"
    assert "DATABASE_URL" in wrong["root_cause_component"]["name"]
    for w in (world_crash, world_oom):
        dx, _ = run(w())
        assert dx["root_cause_component"] == {"name": "backend", "kind": "component",
                                              "relation": "the affected component itself"}


def test_healthy_cluster_is_not_diagnosed():
    dx, _ = run(base_world())
    assert dx["category"] == "undetermined"


def test_secrets_never_appear_in_facts():
    for world in (world_oom(), world_db_misconfig(), world_db_down(), world_crash()):
        _, store = run(world)
        assert all("s3cret" not in f.text and "s3cret" not in str(f.data) for f in store.facts)


def test_no_scenario_parameters():
    """The investigation entry points take no argument that could carry the injected failure."""
    for fn in (collect_mod.collect, diagnosis_mod.diagnose):
        params = set(inspect.signature(fn).parameters)
        assert not params & {"scenario", "failure", "failure_type", "expected"}, fn.__name__


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failed else 0)
