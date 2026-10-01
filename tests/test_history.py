"""Evidence history (Iteration 4): evidence must survive the objects it describes, and missing history must be
stated, never papered over.

The worlds reproduce what the live runs showed: Kubernetes keeps only the current and previous run of a
container, and nothing of a deleted pod. The history store is filled the way the recorder fills it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fake_cluster import NOW, FakeKube, _j, _pod, base_world, world_crash, world_oom  # noqa: E402
from test_scenarios import USER_SYMPTOM  # noqa: E402

from investigator.capabilities import Capabilities, TimeRange  # noqa: E402
from investigator.capabilities.kubernetes import KubernetesAdapter  # noqa: E402
from investigator.collect import collect  # noqa: E402
from investigator.context import IncidentContext  # noqa: E402
from investigator.diagnosis import diagnose  # noqa: E402
from investigator.evidence import EvidenceStore  # noqa: E402
from investigator.history_store import HistoryStore  # noqa: E402
from investigator.planner import plan_and_collect  # noqa: E402
from investigator.report import build, render_text  # noqa: E402

WINDOW = (NOW - 330, NOW)
BACKEND_SIGNAL = [{"kind": "process_restart", "subject": "component/backend", "t": NOW - 30,
                   "text": "Container backend in pod backend-11111 restarted"}]


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def history(tmp_path, start=NOW - 900, max_lines=3000):
    clock = Clock(start)
    return HistoryStore(tmp_path / "history.db", clock=clock, max_lines_per_minute=max_lines), clock


def investigate(world, store=None, strategy="planned", signals=None):
    facts = EvidenceStore(clock=lambda: NOW)
    caps = Capabilities(KubernetesAdapter(FakeKube(world), "shop", history=store), facts)
    incident = {"id": "INC-TEST", "detected_at": NOW - 30, "namespace": "shop",
                "signals": [{**s, "t": s["t"] or NOW - 30} for s in (signals or USER_SYMPTOM)]}
    entry = ("frontend", "8080", "/api/orders")
    if strategy == "exhaustive":
        collect(caps, incident, *WINDOW, entry=entry, log=lambda *_: None)
    else:
        plan_and_collect(caps, IncidentContext.from_incident(incident, *WINDOW, entry=entry, metrics_target="frontend"),
                         log=lambda *_: None)
    dx = diagnose(facts)
    report = build(incident, facts, dx, WINDOW, True)
    render_text(report)
    return dx, facts, report


def record_session(store, clock, start, end):
    clock.t = start
    sid = store.open_session("kubernetes", "shop")
    clock.t = end
    store.heartbeat(sid)


# --------------------------------------------------------------------------- worlds

def world_oom_after_restarts():
    """The live OOM run: both backend pods were OOM-killed three times. The run that took the traffic built up
    memory for minutes and logged it; the runs after it lived seconds. Live "previous" logs are the last
    short run only - no memory warning."""
    w = world_oom()
    for pod in ("backend-11111", "backend-22222"):
        w["logs"][(pod, True)] = [(NOW - 62, _j(level="info", msg="backend starting")),
                                  (NOW - 61, _j(level="info", msg="database connection established"))]
    return w


def fill_oom_history(store, clock):
    record_session(store, clock, NOW - 900, NOW)
    for pod in ("backend-11111", "backend-22222"):
        store.upsert_instance("shop", pod, "backend", "Pod", NOW - 3600)
        # run 1 (generation 0): the long run that took the spike
        store.add_log_lines("shop", "backend", pod, "backend", 0, [
            (NOW - 200, _j(level="info", msg="stats", inflight=12, mem_limit_ratio=0.41)),
            (NOW - 150, _j(level="warning", msg="request backlog growing; workers cannot keep up", inflight=380)),
            (NOW - 125, _j(level="warning", msg="memory usage approaching container limit", mem_limit_ratio=0.86)),
            (NOW - 121, _j(level="warning", msg="memory usage approaching container limit", mem_limit_ratio=0.95))])
        # run 3 (generation 2) is what the live API serves as "previous": the store has it too
        store.add_log_lines("shop", "backend", pod, "backend", 2, [(NOW - 62, _j(level="info", msg="backend starting"))])
        for gen, (started, finished) in enumerate([(NOW - 700, NOW - 120), (NOW - 100, NOW - 95), (NOW - 80, NOW - 60)]):
            store.add_lifecycle("shop", "backend", pod, "backend", "terminated", finished, generation=gen,
                                reason="OOMKilled", exit_code=137, started_at=started, restart_count=gen + 1)


def world_crash_pod_replaced():
    """The live 23:09:51 case: a backend pod crashed, then a rollout replaced it before the investigation."""
    w = world_crash()
    w["pods"][1] = _pod("backend-77777", "backend")      # healthy replacement
    w["pods"][2] = _pod("backend-22222", "backend")
    w["endpoints"]["backend"] = 2
    w["logs"] = {}
    w["events"] = []
    w["entry"] = (200, '{"status": "accepted"}')
    return w


TRACEBACK = ['Traceback (most recent call last):', '  File "/app/backend.py", line 300, in invoice_batch',
             '    unit_price = total / quantity', "decimal.DivisionByZero: [<class 'decimal.DivisionByZero'>]"]


def fill_deleted_pod_history(store, clock, with_logs=True):
    record_session(store, clock, NOW - 900, NOW)
    clock.t = NOW - 600
    store.upsert_instance("shop", "backend-11111", "backend", "Pod", NOW - 3600)
    if with_logs:
        store.add_log_lines("shop", "backend", "backend-11111", "backend", 0,
                            [(NOW - 92, _j(level="info", msg="backend starting"))] + [(NOW - 91, line) for line in TRACEBACK])
    store.add_lifecycle("shop", "backend", "backend-11111", "backend", "terminated", NOW - 90, generation=0,
                        reason="Error", exit_code=1, started_at=NOW - 93, restart_count=1)
    store.mark_gone("shop", "backend-11111", NOW - 80)


# --------------------------------------------------------------------------- A. pod disappears after failure

def test_deleted_pod_evidence_is_retrieved_from_history(tmp_path):
    store, clock = history(tmp_path)
    fill_deleted_pod_history(store, clock)
    for strategy in ("planned", "exhaustive"):
        dx, facts, _ = investigate(world_crash_pod_replaced(), store, strategy, BACKEND_SIGNAL)
        assert dx["category"] == "application_crash", (strategy, dx["category"])
        assert "DivisionByZero" in dx["root_cause"] and "could not be determined" not in dx["root_cause"]
        term = next(f for f in facts.facts if f.kind == "process_terminated")
        assert term.origin == "retained" and term.data["instance_gone"] and term.data["logs_retained"]
        assert "logs of this run were retained" in term.text
        exc = next(f for f in facts.facts if f.kind == "log_exception")
        assert exc.origin == "retained" and exc.data["instance"] == "backend-11111"
        past = next(f for f in facts.facts if f.kind == "past_instance")
        assert past.data["instance"] == "backend-11111" and past.data["retained_runs"] == 1


def test_deleted_pod_without_retained_logs_still_says_why_it_is_unknown(tmp_path):
    store, clock = history(tmp_path)
    fill_deleted_pod_history(store, clock, with_logs=False)
    dx, facts, _ = investigate(world_crash_pod_replaced(), store, signals=BACKEND_SIGNAL)
    assert dx["category"] == "application_crash" and "could not be determined" in dx["root_cause"]
    assert dx["confidence_label"] == "Low"
    past = next(f for f in facts.facts if f.kind == "past_instance")
    assert "no logs of this pod were retained" in past.text


# --------------------------------------------------------------------------- B/C. restarts and the OOM

def test_earlier_runs_come_from_history_and_live_runs_are_not_double_counted(tmp_path):
    store, clock = history(tmp_path)
    fill_oom_history(store, clock)
    adapter = KubernetesAdapter(FakeKube(world_oom_after_restarts()), "shop", history=store)
    runs = adapter.get_log_history("backend", TimeRange(*WINDOW))
    # backend-11111 has restarted 3 times (current run = generation 3): generation 2 is still served live as
    # "previous", so only generation 0 comes from history. backend-22222 has restarted 4 times: its live
    # "previous" is generation 3, so its generation 2 now exists only in history.
    assert sorted((h.instance, h.generation) for h in runs) == [("backend-11111", 0), ("backend-22222", 0),
                                                               ("backend-22222", 2)]
    assert all(h.termination and h.termination.cause == "memory_limit" for h in runs)


def test_retained_memory_evidence_strengthens_the_oom_diagnosis_only_because_it_exists(tmp_path):
    without, facts_without, _ = investigate(world_oom_after_restarts())
    store, clock = history(tmp_path)
    fill_oom_history(store, clock)
    with_history, facts, _ = investigate(world_oom_after_restarts(), store)
    assert without["category"] == with_history["category"] == "memory_exhaustion"
    # The live run's 70%: memory-limit kills (0.6) + repeated kills (0.1), no memory evidence.
    assert without["confidence"] == 0.7
    assert not [f for f in facts_without.facts if f.data.get("signature") == "memory_pressure"]
    # With the retained run, the application's own memory warning is evidence again (+0.15).
    mem = next(f for f in facts.facts if f.data.get("signature") == "memory_pressure")
    assert mem.origin == "retained" and "includes retained logs" in mem.text
    assert with_history["confidence"] == 0.85
    assert mem.id in with_history["evidence"]
    decisions = [s["detail"] for s in facts.trace if s["step"] == "decision"]
    assert "read retained logs of backend" in decisions


def test_retained_logs_are_only_read_when_the_evidence_calls_for_it(tmp_path):
    """No restart beyond the previous run and no instance gone: nothing to look for in history."""
    store, clock = history(tmp_path)
    record_session(store, clock, NOW - 900, NOW)
    _, facts, _ = investigate(world_crash_pod_replaced(), store, signals=BACKEND_SIGNAL)
    assert not [s for s in facts.trace if s["step"] == "capability" and s["detail"] == "get_log_history"]


# --------------------------------------------------------------------------- D. time and configuration history

def test_configuration_changes_carry_exact_or_bounded_times_and_never_secret_values(tmp_path):
    store, clock = history(tmp_path)
    record_session(store, clock, NOW - 900, NOW)
    clock.t = NOW - 600
    store.add_version("shop", "ConfigMap", "backend-config", None, {"DATABASE_URL": "postgresql://shop@postgres:5432/shop"},
                      modified_at=NOW - 3600)
    store.add_version("shop", "Secret", "postgres-credentials", None, {"POSTGRES_PASSWORD": "fp-old"})
    store.add_version("shop", "Deployment", "backend", "backend", {"replicas": 2, "containers": [
        {"name": "backend", "image": "app:0.2", "limits": {"memory": "192Mi"}}]})
    clock.t = NOW - 300                      # last observation before the changes
    store.add_version("shop", "Deployment", "backend", "backend", {"replicas": 2, "containers": [
        {"name": "backend", "image": "app:0.2", "limits": {"memory": "192Mi"}}]})
    store.add_version("shop", "Secret", "postgres-credentials", None, {"POSTGRES_PASSWORD": "fp-old"})
    clock.t = NOW - 295                      # changes observed
    store.add_version("shop", "ConfigMap", "backend-config", None, {"DATABASE_URL": "postgresql://shop@postgres-wrong:5432/shop"},
                      modified_at=NOW - 297)
    store.add_version("shop", "Secret", "postgres-credentials", None, {"POSTGRES_PASSWORD": "fp-new"})
    store.add_version("shop", "Deployment", "backend", "backend", {"replicas": 2, "containers": [
        {"name": "backend", "image": "app:0.2", "limits": {"memory": "128Mi"}}]})
    _, facts, report = investigate(base_world(), store)
    changes = {f.data["item"]: f for f in facts.facts if f.kind == "configuration_change"}
    url = changes["DATABASE_URL"]
    assert url.t == NOW - 297 and url.t_basis == "exact" and "postgres-wrong" in url.text
    limit = changes["containers[backend].limits.memory"]
    assert limit.t_basis == "bounded" and (limit.t_earliest, limit.t_latest) == (NOW - 300, NOW - 295)
    assert "'192Mi' to '128Mi'" in limit.text
    pw = changes["POSTGRES_PASSWORD"]
    assert pw.data["sensitive"] and "value hidden" in pw.text and pw.data["before"] is None
    assert all("fp-" not in f.text for f in facts.facts)
    assert any(t["t_basis"] == "bounded" for t in report["timeline"])


def test_expired_events_come_back_from_history_with_their_own_timestamps(tmp_path):
    store, clock = history(tmp_path)
    record_session(store, clock, NOW - 900, NOW)
    store.upsert_event("shop", "backend", "Pod", "backend-11111", "Warning", "BackOff",
                       "Back-off restarting failed container backend", 5, NOW - 400, NOW - 100)
    _, facts, _ = investigate(base_world(), store)        # the live API no longer has the event
    ev = next(f for f in facts.facts if f.kind == "event")
    assert ev.origin == "retained" and ev.subject == "component/backend"
    assert ev.t_basis == "before_window" and ev.data["first_seen"] == NOW - 400   # not re-dated to the window


# --------------------------------------------------------------------------- G. missing evidence

def test_no_history_is_stated_as_a_limitation():
    _, facts, report = investigate(world_oom())
    gap = next(f for f in facts.facts if f.kind == "evidence_gap")
    assert gap.data["gap"] == "no_history"
    assert report["evidence_coverage"] and "EVIDENCE COVERAGE AND LIMITATIONS" in render_text(report)


def test_partial_coverage_names_the_unrecorded_span(tmp_path):
    store, clock = history(tmp_path)
    record_session(store, clock, NOW - 120, NOW)                  # recording began inside the window
    _, facts, _ = investigate(base_world(), store)
    gaps = [f for f in facts.facts if f.kind == "evidence_gap"]
    assert len(gaps) == 1 and gaps[0].data["gap"] == "not_recorded"
    assert (gaps[0].data["start"], gaps[0].data["end"]) == (WINDOW[0], NOW - 120)
    assert gaps[0].t_basis == "bounded"


def test_dropped_log_lines_are_reported_not_hidden(tmp_path):
    store, clock = history(tmp_path, max_lines=2)
    record_session(store, clock, NOW - 900, NOW)
    store.upsert_instance("shop", "backend-gone", "backend", "Pod", NOW - 500)
    minute = (int(NOW) // 60) * 60 - 240          # the cap is per minute: keep all five lines in one minute
    store.add_log_lines("shop", "backend", "backend-gone", "backend", 0,
                        [(minute + i, _j(level="error", msg=f"failure {i}")) for i in range(5)])
    store.mark_gone("shop", "backend-gone", NOW - 200)
    _, facts, _ = investigate(base_world(), store, signals=BACKEND_SIGNAL)
    gap = next(f for f in facts.facts if f.kind == "evidence_gap" and f.data.get("gap") == "log_lines_dropped")
    assert gap.data["dropped"] == 3 and "unknown" in gap.text
