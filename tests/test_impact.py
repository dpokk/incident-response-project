"""Impact assessment (Iteration 4): every number has a source, and what cannot be measured is said."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fake_cluster import NOW, _pod, base_world, world_crash, world_db_down, world_db_misconfig, world_oom  # noqa: E402
from test_history import (BACKEND_SIGNAL, fill_deleted_pod_history, history, investigate,  # noqa: E402
                          world_crash_pod_replaced)
from test_metrics_correlation import FakeMetrics, climbing_memory, run, series, spike_at  # noqa: E402

from investigator.report import render_text  # noqa: E402


def impact(world, metrics=None, signals=None):
    dx, store, report = run(world, metrics, signals)
    return dx, store, report["impact"], report


def test_measured_user_impact_is_an_explicit_estimate():
    errors = series(lambda t: 0.6 if NOW - 150 <= t < NOW - 40 else 0.0)
    m = FakeMetrics(rps=series(lambda t: 800.0 if t >= NOW - 200 else 100.0), errors=errors,
                    memory={"backend-11111": climbing_memory(NOW - 190, NOW - 120)})
    dx, store, imp, report = impact(world_oom(), m)
    users = imp["users"]
    assert users["affected"] is True and users["quantified"] and users["peak_error_ratio"] == 0.6
    # 800 req/s x 60% failing over the episode: from the first sample above 5% (NOW-150) to the first one back
    # below (NOW-40) = 110 s of 5 s intervals, each weighted by the ratio at its start
    assert users["failed_requests_estimate"] == round(800 * 0.6 * 110)
    assert users["requests_estimate"] == round(800 * 110)
    assert "estimate" in users["statement"]
    assert "user-facing errors lasted" in imp["duration"]["user_facing_errors"]
    assert "IMPACT (evidence-backed" in render_text(report)


def test_without_request_metrics_the_impact_is_not_quantified():
    dx, store, imp, _ = impact(world_oom())
    assert imp["users"]["affected"] is True and imp["users"]["quantified"] is False
    assert imp["users"]["failed_requests_estimate"] is None
    assert "could not be quantified" in imp["users"]["statement"]
    assert any("could not be quantified" in x for x in imp["limitations"])


def test_oom_affected_instances_dependency_and_propagation():
    dx, store, imp, _ = impact(world_oom())
    assert (imp["instances"]["affected"], imp["instances"]["total"]) == (2, 2)
    [dep] = imp["dependencies"]
    assert dep["component"] == "postgres" and dep["state"] == "healthy" and dep["facts"]
    assert [i["component"] for i in imp["impacted"]] == ["frontend"]
    assert imp["propagation"] == "propagated"
    assert imp["duration"]["ongoing"] and imp["duration"]["at_least"]
    roles = {b["component"]: b["role"] for b in imp["blast_radius"]}
    assert roles["backend"] == "affected" and roles["frontend"] == "impacted"


def test_dependency_outage_separates_root_cause_affected_and_impacted():
    dx, store, imp, _ = impact(world_db_down())
    roles = {b["component"]: b["role"] for b in imp["blast_radius"]}
    assert roles == {"postgres": "root cause", "backend": "affected", "frontend": "impacted",
                     "user entry point": "users"}
    assert imp["dependencies"][0]["state"] == "unavailable"
    assert "kept running, but the component was failing" in imp["instances"]["statement"]


def test_a_dependency_that_does_not_exist_is_not_called_unavailable():
    _, _, imp, _ = impact(world_db_misconfig())
    [dep] = imp["dependencies"]
    assert dep["component"] == "postgres-wrong" and dep["state"] == "does not exist"


def test_an_isolated_failure_is_called_isolated():
    w = world_crash()
    w["logs"][("frontend-aaaaa", False)] = []
    w["entry"] = (200, '{"status": "accepted"}')
    m = FakeMetrics(rps=series(lambda t: 100.0), errors=series(lambda t: 0.0))
    dx, store, imp, _ = impact(w, m, signals=BACKEND_SIGNAL)     # only the backend's restarts were detected
    assert dx["category"] == "application_crash"
    assert imp["impacted"] == [] and imp["users"]["affected"] is False
    assert imp["propagation"] == "isolated"
    assert [b["component"] for b in imp["blast_radius"]] == ["backend"]


def test_instances_that_no_longer_exist_are_counted(tmp_path):
    store, clock = history(tmp_path)
    fill_deleted_pod_history(store, clock)
    dx, facts, report = investigate(world_crash_pod_replaced(), store, signals=BACKEND_SIGNAL)
    inst = report["impact"]["instances"]
    assert (inst["affected"], inst["total"], inst["gone"]) == (1, 3, 1)
    assert "1 of them no longer exist" in inst["statement"]


def test_recovered_incident_has_a_bounded_duration(tmp_path):
    store, clock = history(tmp_path)
    fill_deleted_pod_history(store, clock)
    w = world_crash_pod_replaced()
    w["pods"][1] = {**_pod("backend-77777", "backend"), "created": NOW - 70, "ready_since": NOW - 60}
    w["pods"][2] = {**_pod("backend-88888", "backend"), "created": NOW - 70, "ready_since": NOW - 55}
    _, _, report = investigate(w, store, signals=BACKEND_SIGNAL)
    d = report["impact"]["duration"]
    assert not d["ongoing"] and d["end"] == NOW - 55 and d["seconds"] == d["end"] - d["start"]


def test_an_earlier_error_episode_from_another_incident_is_not_counted():
    """Found in the live replay: a database outage's errors (ended 2 min before the first OOM kill) were counted
    as the OOM's failed requests."""
    errors = series(lambda t: 0.9 if NOW - 300 <= t < NOW - 250 or t >= NOW - 70 else 0.0)
    m = FakeMetrics(rps=series(lambda t: 100.0), errors=errors)
    dx, store, imp, report = impact(world_oom(), m)
    eps = store.find(kind="metric_error_ratio")
    assert len(eps) == 2 and [e.data["episode"] for e in eps] == [1, 2]
    assert imp["users"]["facts"] == [eps[1].id]
    assert any("not counted as its impact" in x and eps[0].id in x for x in imp["limitations"])
    onset = next(p for p in report["reconstruction"]["phases"] if p["phase"] == "onset")
    assert onset["facts"] != [eps[0].id]                     # the earlier episode is not this incident's start
    assert any("separate, earlier episode" in u for u in report["reconstruction"]["uncertainties"])


def test_the_same_log_message_in_two_bursts_is_two_observations():
    """Found in the live replay: one frontend error signature spanned a database outage and, after a quiet gap,
    the OOM - and set the OOM's onset 3 minutes too early."""
    from fake_cluster import _j
    w = world_oom()
    line = lambda t: (t, _j(level="error", msg="upstream request to backend failed",  # noqa: E731
                            upstream="http://backend.shop.svc.cluster.local:8080", reason="upstream_http_503"))
    w["logs"][("frontend-aaaaa", False)] = [line(NOW - 300 + i) for i in range(0, 40, 5)] + \
                                          [line(NOW - 62 + i) for i in range(0, 30, 5)]
    _, store, _, report = impact(w)
    sigs = [f for f in store.find(kind="log_signature", subject="component/frontend")]
    assert sorted((s.data["burst"], s.data["bursts"]) for s in sigs) == [(1, 2), (2, 2)]
    first = next(s for s in sigs if s.data["burst"] == 1)
    assert first.data["last"] < NOW - 250
    onset = next(p for p in report["reconstruction"]["phases"] if p["phase"] == "onset")
    assert first.id not in onset["facts"]


def test_a_past_window_does_not_borrow_todays_state(tmp_path):
    """Found in the live replay: pods created (and ready) after the window counted as its instances and as its
    recovery, giving a 711-minute incident."""
    store, clock = history(tmp_path)
    fill_deleted_pod_history(store, clock)
    w = world_crash_pod_replaced()
    later = NOW + 3600                                       # "today": long after the investigated window
    w["pods"][1] = {**_pod("backend-77777", "backend"), "created": later, "ready_since": later + 10}
    w["pods"][2] = {**_pod("backend-88888", "backend"), "created": later, "ready_since": later + 15}
    _, _, report = investigate(w, store, signals=BACKEND_SIGNAL)
    imp = report["impact"]
    assert imp["instances"]["total"] == 1 and imp["instances"]["gone"] == 1
    rec = next(p for p in report["reconstruction"]["phases"] if p["phase"] == "recovery")
    assert "only after the investigated window" in rec["statement"]
    assert imp["duration"]["end"] is None


def test_no_impact_assessment_without_a_diagnosis():
    _, _, imp, report = impact(base_world())
    assert imp["assessed"] is False
    assert "IMPACT" in render_text(report)
