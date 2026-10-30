"""Demo: the console mirrors approval and execution done in Slack - whoever clicked (regression: a Slack approval by
the configured approver was ignored and the page stayed on 'awaiting approval'). Offline; posts nothing."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fake_cluster as fc  # noqa: E402
from test_slack_review import make_plan  # noqa: E402

import demo.engine as engine_mod  # noqa: E402
from demo.bus import EventBus  # noqa: E402
from investigator.config import Settings  # noqa: E402
from investigator.execution_model import ExecutionRequest, Status  # noqa: E402
from investigator.execution_policy import ExecutionPolicy  # noqa: E402
from investigator.execution_store import ExecutionStore  # noqa: E402
from investigator.review import ReviewService  # noqa: E402

APPROVER = "U0APPROVER"
POLICY = os.path.join(os.path.dirname(__file__), "..", "demo", "execution_policy.json")


def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "LogTailer", lambda *a, **k: SimpleNamespace(start=lambda: None, recent=[]))
    bus = EventBus()
    eng = engine_mod.Engine(Settings(), bus, log=lambda m: None)
    eng.transport = SimpleNamespace(configured=False, threaded=False)
    eng.review = ReviewService(tmp_path / "r.db", [APPROVER])
    eng.execution = SimpleNamespace(store=ExecutionStore(tmp_path / "r.db"), policy=ExecutionPolicy.load(POLICY))
    plan, _ = make_plan()
    digest = eng.review.register_plan("INC-TEST", plan, fc.NOW)
    eng.incident = {"id": "INC-TEST", "digest": digest, "proposal": {"plan_index": 0}}
    eng.phase = "awaiting_approval"
    return eng, bus, digest


def kinds(bus):
    return [e["type"] for e in bus._history if e["type"] in ("decision", "remediation", "outcome")]


def test_slack_approve_execute_and_outcome_reach_the_page(tmp_path, monkeypatch):
    eng, bus, d = engine(tmp_path, monkeypatch)
    eng._mirror_slack()
    assert eng.phase == "awaiting_approval" and kinds(bus) == []
    out = eng.review.decide("INC-TEST", d, 0, "approved", APPROVER, supplied="384Mi")
    eng._mirror_slack()
    assert eng.phase == "approved"
    _, rec = eng.execution.store.claim(ExecutionRequest("INC-TEST", d, 0, APPROVER, fc.NOW), "adjust_resource_limit",
                                       out.record, {}, {"allowed": True, "checks": []})
    eng._mirror_slack()
    assert eng.phase == "remediating"
    eng.execution.store.update(rec["execution_id"], Status.COMPLETED, outcome="RESOLVED", message="ok",
                               verification={"criteria": [], "reason": "held"})
    eng._mirror_slack()
    eng._mirror_slack()
    assert eng.phase == "resolved" and kinds(bus).count("decision") == 1 and kinds(bus).count("outcome") == 1


def test_a_slack_rejection_is_shown(tmp_path, monkeypatch):
    eng, bus, d = engine(tmp_path, monkeypatch)
    eng.review.decide("INC-TEST", d, 0, "rejected", APPROVER)
    eng._mirror_slack()
    assert eng.phase == "rejected" and kinds(bus) == ["decision"]


def test_the_evidence_window_never_reaches_back_past_the_last_reset(tmp_path, monkeypatch):
    """Regression: back-to-back incidents - the previous (restored) incident's OOM kills were read as evidence."""
    eng, _, _ = engine(tmp_path, monkeypatch)
    inc = {"detected_at": 10_000.0}
    assert eng._window_start(inc) == 10_000.0 - eng.s.lookback_s          # no reset yet: the usual lookback
    eng.baseline_at = 9_900.0
    assert eng._window_start(inc) == 9_900.0                                # bounded by the reset


def test_acknowledge_on_a_plan_without_a_typed_action_reaches_the_page(tmp_path, monkeypatch):
    """Regression: for a manual-fix incident only Acknowledge exists in Slack, and the page stayed waiting."""
    eng, bus, _ = engine(tmp_path, monkeypatch)
    plan, _ = make_plan(fc.world_crash())
    d = eng.review.register_plan("INC-M", plan, fc.NOW)
    eng.incident = {"id": "INC-M", "digest": d, "proposal": {"executable": False, "plan_index": None}}
    eng.phase = "investigated"
    eng._mirror_slack()
    assert eng.phase == "investigated"
    idx = next(i for i, a in enumerate(plan["actions"]) if a["type"] == "investigate_further")
    assert eng.review.decide("INC-M", d, idx, "acknowledged", APPROVER).effective
    eng._mirror_slack()
    assert eng.phase == "acknowledged" and "decision" in kinds(bus)


# --------------------------------------------------------------------------- the incident's Slack messages

def slack_engine(tmp_path, monkeypatch):
    from test_slack_review import FakeTransport

    from investigator import slack
    eng, bus, d = engine(tmp_path, monkeypatch)
    eng.transport, eng.registry = FakeTransport(), slack.ThreadRegistry(tmp_path / "threads.json")
    slack.publish_detection(eng.transport, eng.registry, {"id": "INC-TEST", "signals": [{"text": "5xx"}]}, "shop")
    eng.registry.set("INC-TEST", plan_ts="ts-plan")
    eng.incident.update({"detected_at": fc.NOW - 200, "cause": "the limit is too low", "fix": "Raise the limit"})
    return eng, d


def headers(tr):
    return [b["text"]["text"] for _, p in tr.posts for b in p["blocks"] if b["type"] == "header"]


def test_a_resolved_execution_ends_with_its_own_final_status_message(tmp_path, monkeypatch):
    eng, d = slack_engine(tmp_path, monkeypatch)
    out = eng.review.decide("INC-TEST", d, 0, "approved", APPROVER, supplied="384Mi")
    eng._mirror_slack()
    assert "Approved by" in str(eng.transport.updates[-1][1])                  # the detection status follows
    _, rec = eng.execution.store.claim(ExecutionRequest("INC-TEST", d, 0, APPROVER, fc.NOW), "adjust_resource_limit",
                                       out.record, {}, {"allowed": True, "checks": []})
    eng.execution.store.update(rec["execution_id"], Status.COMPLETED, outcome="RESOLVED", message="ok",
                               verification={"criteria": [{"statement": "All backend instances are ready",
                                                           "status": "pass"}], "reason": "held"})
    eng._mirror_slack()
    final = eng.transport.posts[-1]
    assert final[0] is None and headers(eng.transport)[-1] == ":white_check_mark: Resolved · INC-TEST"
    text = str(final[1])
    assert "the limit is too low" in text and "1/1 checks held" in text and "Duration:" in text
    assert eng.registry.get("INC-TEST")["final_ts"] and "*Status:* :white_check_mark: Resolved" in \
        str(eng.transport.updates[-1][1])


def test_a_rejection_ends_with_a_final_status(tmp_path, monkeypatch):
    eng, d = slack_engine(tmp_path, monkeypatch)
    eng.review.decide("INC-TEST", d, 0, "rejected", APPROVER)
    eng._mirror_slack()
    assert headers(eng.transport)[-1] == ":no_entry: Fix not applied · INC-TEST" and eng.transport.posts[-1][0] is None


def test_the_investigation_message_carries_the_agents_verdict(tmp_path):
    from demo import slack_ai
    _, report = make_plan()
    report["id"] = "INC-TEST"
    inc = {"id": "INC-TEST", "route": {"mode": "verify"}}
    agree = {"status": "ok", "tool_calls": 4, "seconds": 48, "model": "m",
             "report": {"root_cause": "x", "root_cause_component": "backend", "findings": [], "summary": "",
                        "validation": {"ok": True, "problems": []}, "agreement": {"category": True, "component": True}}}
    t = str(slack_ai.investigation(inc, report, agree))
    assert "Investigation · INC-TEST" in t and "agrees" in t and report["likely_root_cause"][:40] in t
    assert "AI agent unavailable" in str(slack_ai.investigation(inc, report, {"status": "error", "error": "timeout"}))
    manual = str(slack_ai.manual({"id": "INC-TEST", "cause": "bad data"},
                                 {"reason": "no typed action fits", "manual_steps": ["Delete the bad order"]}))
    assert "Handed to engineer" in manual and "1. Delete the bad order" in manual and "Acknowledge" in manual
