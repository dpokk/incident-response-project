"""Timeline reconstruction (Iteration 4): phases and order inferred from facts, never more precise than the
evidence, never claiming what was not observed."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fake_cluster import NOW, _pod  # noqa: E402
from test_history import (BACKEND_SIGNAL, fill_deleted_pod_history, fill_oom_history, history, investigate,  # noqa: E402
                          world_crash_pod_replaced, world_oom_after_restarts)

from investigator.evidence import EvidenceStore  # noqa: E402
from investigator.report import render_text  # noqa: E402
from investigator.timeline import reconstruct  # noqa: E402

T0 = 1_000_000.0
WINDOW = (T0, T0 + 600)
DX = {"affected_component": "backend", "root_cause_component": {"name": "backend", "kind": "component"},
      "impacted_components": ["frontend"]}


def store_with(*adds):
    s = EvidenceStore(clock=lambda: T0 + 600)
    for kind, subject, t, kw in adds:
        s.add("test", subject, kind, f"{kind} on {subject}", t=t, **kw)
    return s


def healthy_backend(ready_since, errors_last=None):
    adds = [("component_status", "component/backend", None, {"ready": 2, "desired": 2}),
            ("instance_status", "component/backend", None, {"ready": True, "ready_since": ready_since, "restarts": 1}),
            ("instance_status", "component/backend", None, {"ready": True, "ready_since": ready_since - 5, "restarts": 0})]
    return adds


def term(t):
    return ("process_terminated", "component/backend", t, {"cause": "memory_limit"})


def test_phases_follow_the_evidence_in_order():
    s = store_with(("log_signature", "component/backend", T0 + 100, {"signature": "memory_pressure"}),
                   term(T0 + 130), term(T0 + 160),
                   ("log_signature", "component/frontend", T0 + 131, {"signature": "upstream_failure", "last": T0 + 200}),
                   *healthy_backend(T0 + 300))
    rc = reconstruct(s, DX, WINDOW)
    assert [p["phase"] for p in rc["phases"]] == ["baseline", "onset", "development", "failure", "recovery", "post_incident"]
    onset = next(p for p in rc["phases"] if p["phase"] == "onset")
    assert onset["start"] == {"earliest": T0 + 100, "latest": T0 + 100, "basis": "exact"} and onset["facts"] == ["F1"]
    failure = next(p for p in rc["phases"] if p["phase"] == "failure")
    assert (failure["start"]["earliest"], failure["end"]["latest"]) == (T0 + 130, T0 + 160)
    assert rc["window"]["incident_start"]["earliest"] == T0 + 100 and rc["window"]["incident_end"]["latest"] == T0 + 300
    assert "F1" in rc["answers"]["immediately_before_failure"]["facts"]
    [r1, r2, r3] = rc["relations"]
    assert r1["order"] == "before" and "by 30s" in r1["statement"] and "not cause" in r1["statement"]
    assert r2["order"] == "before"         # failure (130) before the frontend's first error (131)
    assert r3["order"] == "before"         # last failure (160) before recovery (300)


def test_overlapping_times_give_no_order():
    """A change known only to lie in [100, 140] and a failure at 120: which came first is unknown."""
    s = store_with(("configuration_change", "component/backend", T0 + 140,
                    {"t_basis": "bounded", "t_earliest": T0 + 100, "t_latest": T0 + 140}),
                   ("log_signature", "component/backend", T0 + 120, {"signature": "connection_refused"}))
    rc = reconstruct(s, DX, WINDOW)
    [rel] = [r for r in rc["relations"] if r["from"] == "F1"]
    assert rel["order"] == "undetermined" and "cannot be determined" in rel["statement"]
    assert "–" in rc["answers"]["what_changed_first"]["statement"]       # the range, never a single "at" time


def test_times_known_only_as_upper_bounds_are_reported_that_way():
    s = store_with(("log_signature", "component/backend", T0 + 50, {"signature": "connection_refused",
                                                                     "t_basis": "observed"}))
    rc = reconstruct(s, DX, WINDOW)
    assert "at or before" in rc["answers"]["symptoms_began"]["statement"]
    assert any("may have begun earlier" in u for u in rc["uncertainties"])


def test_undated_observations_are_not_placed_in_the_sequence():
    s = store_with(("log_exception", "component/backend", None, {}), term(T0 + 200))
    rc = reconstruct(s, DX, WINDOW)
    assert rc["undated"] == ["F1"] and all(e["id"] != "F1" for e in rc["entries"])
    assert any("no known time" in u for u in rc["uncertainties"])


def test_changes_after_the_failure_are_not_offered_as_what_changed_first():
    s = store_with(term(T0 + 100), ("change", "component/backend", T0 + 200, {}))
    rc = reconstruct(s, DX, WINDOW)
    assert rc["answers"]["what_changed_first"]["facts"] == []
    assert [c["id"] for c in rc["changes_after_failure"]] == ["F2"]


def test_what_changed_first_prefers_the_lead_up_and_dates_older_changes():
    old_and_new = store_with(("change", "component/backend", T0 - 900, {}), ("change", "component/backend", T0 + 20, {}),
                             term(T0 + 100))
    rc = reconstruct(old_and_new, DX, WINDOW)
    assert rc["answers"]["what_changed_first"]["facts"] == ["F2"]
    assert "lead-up" in rc["answers"]["what_changed_first"]["statement"]
    only_old = reconstruct(store_with(("change", "component/backend", T0 - 900, {}), term(T0 + 100)), DX, WINDOW)
    assert "17 min before the onset" in only_old["answers"]["what_changed_first"]["statement"]


def test_recurring_observations_from_before_the_window_do_not_set_the_onset():
    s = store_with(("event", "component/backend", T0 + 300, {"t_basis": "before_window", "first_seen": T0 - 3000,
                                                              "category": "health_check_failed"}),
                   ("log_signature", "component/backend", T0 + 200, {"signature": "connection_refused"}))
    rc = reconstruct(s, DX, WINDOW)
    assert next(p for p in rc["phases"] if p["phase"] == "onset")["facts"] == ["F2"]
    assert any("already recurring before the window" in u for u in rc["uncertainties"])


def test_recovery_needs_readiness_after_the_failure_and_errors_to_stop():
    ready_before = reconstruct(store_with(term(T0 + 100), *healthy_backend(T0 + 50)), DX, WINDOW)
    rec = next(p for p in ready_before["phases"] if p["phase"] == "recovery")
    assert rec["start"] is None and "not known" in rec["statement"]
    still_failing = reconstruct(store_with(term(T0 + 100), *healthy_backend(T0 + 300),
                                           ("log_signature", "component/backend", T0 + 310,
                                            {"signature": "connection_refused", "last": T0 + 590})), DX, WINDOW)
    rec = next(p for p in still_failing["phases"] if p["phase"] == "recovery")
    assert "not recovered" in rec["statement"] and still_failing["window"]["ongoing"]


def test_unrecorded_failures_are_admitted():
    s = store_with(("event", "component/backend", T0 + 50, {"category": "restart_backoff"}), term(T0 + 200),
                   ("instance_status", "component/backend", None, {"ready": False, "restarts": 5}))
    rc = reconstruct(s, DX, WINDOW)
    assert any("restarted 5 time(s) but only 1 termination" in u for u in rc["uncertainties"])
    assert any("began earlier than the evidence shows" in u for u in rc["uncertainties"])
    assert rc["answers"]["failure_occurred"]["statement"].startswith("The earliest recorded failure")


def test_reconstruction_adds_no_facts():
    s = store_with(term(T0 + 100))
    before = len(s.facts)
    reconstruct(s, DX, WINDOW)
    assert len(s.facts) == before


# --------------------------------------------------------------------------- end to end, with retained history

def test_oom_reconstruction_uses_the_retained_memory_warning(tmp_path):
    store, clock = history(tmp_path)
    fill_oom_history(store, clock)
    dx, facts, report = investigate(world_oom_after_restarts(), store)
    rc = report["reconstruction"]
    onset = next(p for p in rc["phases"] if p["phase"] == "onset")
    onset_fact = facts.by_id(onset["facts"][0])
    # The first sign is the memory warning that only the retained earlier run contains.
    assert onset_fact.data["signature"] == "memory_pressure" and onset_fact.origin == "retained"
    assert onset_fact.id in rc["answers"]["immediately_before_failure"]["facts"]
    first_failure = facts.by_id(rc["answers"]["failure_occurred"]["facts"][0])
    assert first_failure.kind == "process_terminated" and first_failure.data["cause"] == "memory_limit"
    rel = next(r for r in rc["relations"] if r["from"] == onset_fact.id and r["to"] == first_failure.id)
    assert rel["order"] == "before" and "by 5s" in rel["statement"]
    assert rc["window"]["ongoing"]                                     # the pods are not ready: no recovery claimed
    assert "RECONSTRUCTED TIMELINE" in render_text(report)


def test_crash_after_pod_replacement_separates_the_rollout_from_the_failure(tmp_path):
    store, clock = history(tmp_path)
    fill_deleted_pod_history(store, clock)
    w = world_crash_pod_replaced()
    w["pods"][1] = {**_pod("backend-77777", "backend"), "created": NOW - 70, "ready_since": NOW - 60}
    w["pods"][2] = {**_pod("backend-88888", "backend"), "created": NOW - 70, "ready_since": NOW - 55}
    w["replicasets"].append({"name": "backend-rs2", "deployment": "backend", "created": NOW - 70, "revision": "2",
                             "replicas": 2, "ready": 2})
    dx, facts, report = investigate(w, store, signals=BACKEND_SIGNAL)
    rc = report["reconstruction"]
    assert dx["category"] == "application_crash"
    rollout = next(f for f in facts.facts if f.kind == "change")
    assert rollout.id in [c["id"] for c in rc["changes_after_failure"]]
    assert rollout.id not in rc["answers"]["what_changed_first"]["facts"]
    rec = next(p for p in rc["phases"] if p["phase"] == "recovery")
    assert rec["start"]["earliest"] == NOW - 55 and rc["window"]["incident_end"]["latest"] == NOW - 55
