"""Temporal semantics in the final report (Iteration 4 finalization): a time is never shown more precisely than
the evidence knows it, durations from uncertain times stay bounds, and event / observation / collection time
stay distinct."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fake_cluster import NOW, _j  # noqa: E402
from test_history import BACKEND_SIGNAL, history, investigate, record_session, world_crash_pod_replaced  # noqa: E402

from investigator.evidence import EvidenceStore  # noqa: E402
from investigator.report import build, hms, reconstruction_lines, render_text  # noqa: E402
from investigator.timeline import duration, exact, fmt_moment, moment, reconstruct  # noqa: E402

T0 = 1_000_000.0
WINDOW = (T0, T0 + 600)
DX = {"affected_component": "backend", "root_cause_component": {"name": "backend", "kind": "component", "relation": "the affected component itself"},
      "impacted_components": [], "category": "memory_exhaustion"}


def store_with(*adds):
    s = EvidenceStore(clock=lambda: T0 + 600)
    for kind, subject, t, kw in adds:
        s.add("test", subject, kind, f"{kind} on {subject}", t=t, **kw)
    return s


def bounded_onset_store():
    return store_with(("metric_error_ratio", "service/frontend", T0 + 140,
                       {"t_basis": "bounded", "t_earliest": T0 + 120, "t_latest": T0 + 140, "component": "frontend",
                        "peak": 0.9}),
                      ("process_terminated", "component/backend", T0 + 200, {"cause": "memory_limit", "instance": "backend-1"}),
                      ("component_status", "component/backend", None, {"ready": 0, "desired": 2}),
                      ("instance_status", "component/backend", None, {"ready": False, "restarts": 1, "instance": "backend-1"}))


def phase(rc, name):
    return next(p for p in rc["phases"] if p["phase"] == name)


def line(lines, label):
    return next(x for x in lines if x.strip().startswith(label))


# 1. bounded stays bounded in the final report -----------------------------------------------------------------

def test_a_bounded_onset_is_shown_as_its_range_never_as_its_earliest_bound():
    rc = reconstruct(bounded_onset_store(), DX, WINDOW)
    onset = phase(rc, "onset")
    assert onset["start"] == {"earliest": T0 + 120, "latest": T0 + 140, "basis": "bounded"}
    rng = f"{hms(T0 + 120)}–{hms(T0 + 140)}"
    lines = reconstruction_lines(rc)
    assert rng in line(lines, "Onset")
    assert f"from {rng} to ongoing" in line(lines, "Window:")          # the incident's start is the same range
    assert rc["window"]["incident_start"]["basis"] == "bounded"


def test_bounded_facts_are_ranges_in_the_observed_entries():
    s = bounded_onset_store()
    text = render_text(build({"id": "X", "signals": []}, s, {**DX, **_dx_fields()}, WINDOW, True))
    observed = text.split("TIMELINE (observed entries)")[1]
    assert f"{hms(T0 + 120)}–{hms(T0 + 140)}" in observed


# 2. exact stays exact --------------------------------------------------------------------------------------------

def test_an_exact_time_is_shown_as_one_time():
    rc = reconstruct(store_with(("process_terminated", "component/backend", T0 + 200, {"cause": "memory_limit", "instance": "backend-1"})),
                     DX, WINDOW)
    assert phase(rc, "onset")["start"] == exact(T0 + 200)
    onset_line = line(reconstruction_lines(rc), "Onset")
    assert hms(T0 + 200) in onset_line and "–" not in onset_line.split("First sign")[0]
    assert fmt_moment(exact(T0 + 200)) == hms(T0 + 200)


# 3. durations from bounded times stay bounds ---------------------------------------------------------------------

def test_duration_between_bounded_times_is_a_range():
    d = duration(moment((T0 + 120, T0 + 140), "bounded"), exact(T0 + 300))
    assert (d["min_s"], d["max_s"]) == (160, 180) and d["statement"] == "between 2m 40s and 3m 00s"


def test_duration_of_an_ongoing_incident_is_only_a_lower_bound_from_the_latest_possible_start():
    d = duration(moment((T0 + 120, T0 + 140), "bounded"), None, ongoing_at=T0 + 260)
    assert (d["min_s"], d["max_s"]) == (120, None) and d["statement"].startswith("at least 2m 00s")


def test_duration_with_an_open_start_has_no_upper_bound():
    d = duration(moment((None, T0 + 140), "observed"), exact(T0 + 300))
    assert d["max_s"] is None and d["statement"] == "at least 2m 40s"
    assert duration(exact(T0), exact(T0 + 95))["statement"] == "1m 35s"     # exact ends: one number


def test_report_duration_preserves_the_bound():
    from investigator.impact import assess
    s = bounded_onset_store()
    rc = reconstruct(s, DX, WINDOW)
    imp = assess(s, {**DX, **_dx_fields()}, rc)
    assert imp["duration"]["max_s"] is None and imp["duration"]["statement"].startswith("at least 7m 40s")
    assert f"{hms(T0 + 120)}–{hms(T0 + 140)}" in imp["duration"]["statement"]


# 4. pre-window events get no artificial time ---------------------------------------------------------------------

def test_pre_window_events_are_not_given_a_start_time():
    s = store_with(("event", "component/backend", T0 + 300,
                    {"t_basis": "before_window", "first_seen": T0 - 5000, "category": "health_check_failed"}),
                   ("process_terminated", "component/backend", T0 + 200, {"cause": "memory_limit", "instance": "backend-1"}))
    rc = reconstruct(s, DX, WINDOW)
    assert phase(rc, "onset")["facts"] == ["F2"]
    assert rc["window"]["incident_start"] == exact(T0 + 200)          # not the event's first sighting at T0-5000
    text = render_text(build({"id": "X", "signals": []}, s, {**DX, **_dx_fields()}, WINDOW, True))
    observed = text.split("TIMELINE (observed entries)")[1]
    assert "before window" in observed and hms(T0 - 5000) not in observed.split("event on")[0]


# 5. event, observation and collection time stay distinct ---------------------------------------------------------

def test_event_observation_and_collection_times_are_kept_apart(tmp_path):
    store, clock = history(tmp_path)
    record_session(store, clock, NOW - 900, NOW)
    store.upsert_instance("shop", "backend-11111", "backend", "Pod", NOW - 3600)
    store.add_log_lines("shop", "backend", "backend-11111", "backend", 0, [(NOW - 92, _j(level="info", msg="x"))])
    clock.t = NOW - 85                                   # the recorder saw the termination 5 s after it happened
    store.add_lifecycle("shop", "backend", "backend-11111", "backend", "terminated", NOW - 90, generation=0,
                        reason="Error", exit_code=1, started_at=NOW - 93, restart_count=1)
    store.mark_gone("shop", "backend-11111", NOW - 80)
    _, facts, report = investigate(world_crash_pod_replaced(), store, signals=BACKEND_SIGNAL)
    term = next(f for f in facts.facts if f.kind == "process_terminated")
    assert (term.t, term.observed_at, term.collected_at) == (NOW - 90, NOW - 85, NOW)
    entry = next(e for e in report["timeline"] if e["id"] == term.id)
    assert (entry["t"], entry["observed_at"], entry["collected_at"]) == (NOW - 90, NOW - 85, NOW)
    live = next(f for f in facts.facts if f.kind == "component_status")
    assert live.observed_at is None and live.collected_at == NOW     # a live read: observed when collected


def _dx_fields():
    return {"category_label": "Memory exhaustion", "dependencies": [], "symptoms": [], "evidence": [], "reasoning": [],
            "root_cause": "x", "contributing_factors": [], "confidence": 0.5, "confidence_label": "Medium",
            "alternatives": [], "component_summary": [], "correlations": []}
