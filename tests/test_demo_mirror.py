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
