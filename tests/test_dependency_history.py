"""Recovered dependency outage (Iteration 4 finalization): PostgreSQL went down and came back before the
investigation. Live state is healthy; only recorded availability history shows the outage. The investigator is
never told that PostgreSQL failed."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fake_cluster import NOW, _j, _pod, base_world  # noqa: E402
from test_history import history, investigate, record_session  # noqa: E402

from investigator.capabilities.kubernetes_recorder import ENDPOINTS_KIND, STATUS_KIND  # noqa: E402
from investigator.report import render_text  # noqa: E402

REFUSED = 'connection failed: connection to server at "10.96.0.12", port 5432 failed: Connection refused'


def world_db_recovered():
    """After the outage: postgres is back (a new pod, ready since NOW-95); backend pods never restarted but logged
    connection errors from NOW-300 to NOW-110; the frontend saw 503s; users are fine now."""
    w = base_world()
    w["pods"][3] = {**_pod("postgres-44444", "postgres", mem_limit=512 * 2**20, cpu=None),
                    "created": NOW - 100, "ready_since": NOW - 95}
    for pod in ("backend-11111", "backend-22222"):
        w["logs"][(pod, False)] = [(NOW - 300 + i * 10, _j(level="error", msg="database connection failed",
                                                          db_host="postgres", db_port="5432",
                                                          error_type="OperationalError", error=REFUSED))
                                   for i in range(20)]
    w["logs"][("frontend-aaaaa", False)] = [(NOW - 290 + i * 20, _j(level="error", msg="upstream request to backend failed",
                                                                   upstream="http://backend.shop.svc.cluster.local:8080",
                                                                   reason="upstream_http_503", count=200,
                                                                   sample_error='{"error": "database unavailable"}'))
                                            for i in range(9)]
    return w


def record(store, clock, t, ready, desired):
    clock.t = t
    store.add_version("shop", ENDPOINTS_KIND, "postgres", None, {"ready": ready, "not_ready": 0})
    store.add_version("shop", STATUS_KIND, "postgres", "postgres", {"desired": desired, "ready": ready})


def fill_outage(store, clock, down_at=NOW - 305, up_at=NOW - 95):
    record_session(store, clock, NOW - 900, NOW)
    record(store, clock, NOW - 800, 1, 1)
    record(store, clock, down_at - 5, 1, 1)                  # last seen up
    record(store, clock, down_at, 0, 0)                      # first seen down (scaled to zero)
    record(store, clock, up_at - 5, 0, 1)                    # scaled back up, not ready yet
    record(store, clock, up_at, 1, 1)                        # first seen ready again
    record(store, clock, NOW - 10, 1, 1)


def test_recovered_postgres_outage_is_reconstructed_from_history(tmp_path):
    store, clock = history(tmp_path)
    fill_outage(store, clock)
    for strategy in ("exhaustive", "planned"):                       # planned last: its trace is checked below
        dx, facts, report = investigate(world_db_recovered(), store, strategy)
        assert dx["category"] == "dependency_unavailable", (strategy, dx["category"])
        assert dx["affected_component"] == "backend"
        assert dx["root_cause_component"]["name"] == "postgres"
        assert dx["impacted_components"] == ["frontend"]
        outage = next(f for f in facts.facts if f.kind == "availability_outage")
        assert outage.origin == "retained" and outage.id in dx["evidence"]
        assert (outage.t_earliest, outage.t_latest) == (NOW - 310, NOW - 305)          # bounded by two polls
        assert outage.data["scaled_to_zero"] and "recovered since" in dx["root_cause"]
        # live state is healthy and is reported as such, not used against the diagnosis
        eps = next(f for f in facts.facts if f.kind == "service_endpoints" and f.subject == "dependency/postgres:5432")
        assert eps.data["ready"] == 1 and any("recovered since" in r["statement"] for r in dx["reasoning"])
    decisions = [s["detail"] for s in facts.trace if s["step"] == "decision"]
    assert "read availability history of postgres:5432" in decisions
    rc = report["reconstruction"]
    failure = next(p for p in rc["phases"] if p["phase"] == "failure")
    assert outage.id in failure["facts"]
    recovery = next(p for p in rc["phases"] if p["phase"] == "recovery")
    assert recovery["start"]["earliest"] == NOW - 95                 # postgres's new pod ready again
    dep = report["impact"]["dependencies"][0]
    assert dep["state"] == "unavailable during the incident" and "healthy at investigation" in dep["statement"]
    text = render_text(report)
    assert "Recorded history: service postgres" in text and "–" in text


def test_a_crash_right_after_the_recorded_recovery_is_not_called_unrelated_to_it(tmp_path):
    """Found in the live demonstration: a backend instance exited 1 s after PostgreSQL came back. "Its dependencies
    are reachable (now), so the crash is not a dependency outage" was wrong once history showed the outage."""
    from investigator.diagnosis import View, check_crash
    w = world_db_recovered()
    last = {"state": "terminated", "reason": "Error", "exit_code": 1, "signal": None, "message": None,
            "started_at": NOW - 2000, "finished_at": NOW - 94}
    w["pods"][2] = {**_pod("backend-22222", "backend", restarts=1, last=last)}
    w["logs"][("backend-22222", True)] = [(NOW - 94, line) for line in (
        "Traceback (most recent call last):", '  File "/app/backend.py", line 147, in connection',
        "_queue.Empty")]
    store, clock = history(tmp_path)
    fill_outage(store, clock)
    dx, facts, _ = investigate(w, store)
    assert dx["category"] == "dependency_unavailable" and dx["root_cause_component"]["name"] == "postgres"
    dep_health = {"backend": [{"endpoint": "postgres:5432", "healthy": True, "fact": None,
                               "outages": facts.find(kind="availability_outage")}]}
    crash = check_crash(View(facts, "backend"), facts, dep_health)
    said = " ".join(r["statement"] for r in crash.reasoning)
    assert "reachable" not in said and "may be linked to that outage or to its recovery" in said


def test_without_history_the_recovered_outage_is_not_claimed():
    """The gap this closes: live state is healthy, so without history the outage is invisible (and the healthy
    state even argues against it). The investigator says Undetermined rather than guessing."""
    dx, facts, report = investigate(world_db_recovered())
    assert dx["category"] == "undetermined" and dx["confidence"] == 0.0
    una = next(a for a in dx["alternatives"] if a["category"] == "dependency_unavailable" and a["component"] == "backend")
    assert "ready endpoint" in una["why_not"]
    assert any(f.kind == "evidence_gap" and f.data.get("gap") == "no_history" for f in facts.facts)


def test_history_that_shows_postgres_stayed_up_argues_against_an_outage(tmp_path):
    store, clock = history(tmp_path)
    record_session(store, clock, NOW - 900, NOW)
    for t in (NOW - 800, NOW - 300, NOW - 10):
        record(store, clock, t, 1, 1)
    dx, facts, _ = investigate(world_db_recovered(), store)
    assert dx["category"] != "dependency_unavailable"
    steady = next(f for f in facts.facts if f.kind == "availability_steady")
    una = next(a for a in dx["alternatives"] if a["category"] == "dependency_unavailable" and a["component"] == "backend")
    assert "stayed available" in una["why_not"] and steady.origin == "retained"


def test_an_outage_that_did_not_overlap_the_errors_does_not_explain_them(tmp_path):
    store, clock = history(tmp_path)
    fill_outage(store, clock, down_at=NOW - 700, up_at=NOW - 650)    # long before the errors (NOW-300..NOW-110)
    dx, facts, _ = investigate(world_db_recovered(), store)
    assert dx["category"] != "dependency_unavailable" or not any(
        facts.by_id(i).kind == "availability_outage" for i in dx["evidence"])
