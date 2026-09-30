"""Planner tests (Iteration 3): the adaptive investigation must reach the same evidence-driven diagnosis as
the exhaustive Iteration 2 procedure, and must decide which evidence is relevant (and say why).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from fake_cluster import base_world, world_crash, world_db_down, world_db_misconfig, world_oom  # noqa: E402
from test_scenarios import run  # noqa: E402

WORLDS = [world_oom, world_db_misconfig, world_db_down, world_crash, base_world]


def decisions(store) -> list[str]:
    return [f"{s['detail']} | {s.get('reason', '')}" for s in store.trace if s["step"] == "decision"]


def calls(store, capability=None) -> list[dict]:
    return [s for s in store.trace if s["step"] == "capability" and (capability is None or s["detail"] == capability)]


def test_planned_matches_exhaustive_diagnosis():
    for w in WORLDS:
        planned, _ = run(w(), "planned")
        exhaustive, _ = run(w(), "exhaustive")
        for key in ("category", "affected_component", "impacted_components"):
            assert planned[key] == exhaustive[key], (w.__name__, key, planned[key], exhaustive[key])
        assert [d.get("host") for d in planned["dependencies"]] == [d.get("host") for d in exhaustive["dependencies"]]
        assert abs(planned["confidence"] - exhaustive["confidence"]) <= 0.05, (w.__name__, planned["confidence"],
                                                                              exhaustive["confidence"])


def test_every_decision_has_a_reason():
    for w in WORLDS:
        _, store = run(w())
        ds = [s for s in store.trace if s["step"] == "decision"]
        assert ds and all(s.get("reason") for s in ds), w.__name__


def test_follows_errors_down_the_dependency_chain():
    """Only the frontend is named by the signal; the planner must reach postgres through the evidence."""
    _, store = run(world_db_down())
    d = decisions(store)
    assert any(x.startswith("examine backend") and "frontend" in x for x in d), d
    assert any(x.startswith("examine postgres") for x in d), d
    assert any(x.startswith("test connectivity backend -> postgres:5432") for x in d), d


def test_previous_logs_only_when_something_restarted():
    _, crash = run(world_crash())
    assert any(c["kwargs"].get("previous") == "True" for c in calls(crash, "get_logs"))
    _, down = run(world_db_down())
    assert not any(c["kwargs"].get("previous") == "True" for c in calls(down, "get_logs"))


def test_no_connectivity_probe_without_a_reason():
    """The crash is internal to the backend: no reason to probe the database from it."""
    _, store = run(world_crash())
    probed = [c["args"] for c in calls(store, "check_connectivity")]
    assert not any(a[0] == "backend" for a in probed), probed


def test_metrics_only_when_resource_exhaustion_is_suspected():
    _, oom = run(world_oom())
    _, down = run(world_db_down())
    assert any("resource exhaustion suspected" in x for x in decisions(oom))
    assert any(x.startswith("skip metrics | no sign of resource exhaustion") for x in decisions(down))


def test_survey_when_no_component_is_named():
    """A manual investigation names no component: the planner examines everything."""
    manual = [{"kind": "manual", "subject": "system", "t": None, "text": "manual investigation request"}]
    dx, store = run(world_db_misconfig(), signals=manual)
    assert dx["category"] == "dependency_misconfiguration"
    assert any("survey everything" in x for x in decisions(store))


def test_planned_uses_no_more_capability_calls_than_exhaustive():
    for w in WORLDS:
        _, planned = run(w(), "planned")
        _, exhaustive = run(w(), "exhaustive")
        assert len(calls(planned)) <= len(calls(exhaustive)), (w.__name__, len(calls(planned)), len(calls(exhaustive)))
