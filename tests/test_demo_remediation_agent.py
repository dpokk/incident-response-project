"""Demo: the Remediation Agent - the policy gate, the attempt ladder, retries, the revert, Stop, the lock and its
acknowledgement. The executor is replaced by a scripted fake that records every request (the real executor's own checks
are covered by the Iteration 7 tests); the review service, the plans and the rollback plan are real."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fake_cluster as fc  # noqa: E402
from test_slack_review import make_plan  # noqa: E402

from demo.remediation_agent import (AGENT_ID, AutoPolicy, AutoStore, RemediationAgent, REVERT_KEY,  # noqa: E402
                                    gate, ladder, retry_plan)
from investigator.execution_model import ExecutionResult  # noqa: E402
from investigator.executor import ExecutionService  # noqa: E402
from investigator.review import ReviewService  # noqa: E402

HUMAN = "U0HUMAN"
POLICY = os.path.join(os.path.dirname(__file__), "..", "demo", "remediation_agent_policy.json")
GiB = 2**30


# --------------------------------------------------------------------------- fixtures

def good_inputs(action_type="adjust_resource_limit", value="256Mi"):
    route = {"mode": "verify", "rule_confidence": 0.97}
    ai = {"status": "ok", "report": {"agreement": {"category": True, "component": True},
                                     "validation": {"ok": True, "findings": 4, "supported_findings": 4}}}
    proposal = {"executable": True, "plan_index": 0, "action_type": action_type, "target": {"component": "backend"},
                "summary": "s", "parameter": {"name": "x", "value": value}}
    return route, ai, proposal


class FakeExecution:
    """Scripted outcomes, one per execute() call: "RESOLVED", "NOT_RESOLVED", "INCONCLUSIVE", "refused", "uncertain".
    Each call checks that an effective approval by the agent exists for exactly that request, like the executor."""

    def __init__(self, review, script):
        self.review, self.script, self.calls, self.records = review, list(script), [], {}
        self.store = self
        self.policy = type("P", (), {"max_memory_bytes": GiB})()

    def get(self, eid):
        return self.records.get(eid)

    def execute(self, req, progress=None):
        d = self.review.effective_decision(req.incident_id, req.plan_digest, req.action_index)
        assert d and d["decision"] == "approved" and d["reviewer"] == AGENT_ID and req.executor == AGENT_ID
        plan = self.review.plan(req.incident_id, req.plan_digest)["plan"]
        change, code, msg = ExecutionService.change_for(plan, req.action_index, d)   # the real typed mapping
        assert change is not None, msg
        self.calls.append((req.incident_id, change))
        eid = f"e{len(self.calls)}"
        what = self.script.pop(0)
        before, after = _values(change)
        if progress:
            progress("checks_passed", {"execution_id": eid, "change": change.describe(), "checks": []})
        if what == "refused":
            self.records[eid] = {"status": "refused", "message": "state changed"}
            return ExecutionResult(False, "EXECUTION_REFUSED_STATE_CHANGED", "state changed", eid)
        applied = {"accepted": True, "before": before, "after": after}
        if what == "uncertain":
            self.records[eid] = {"status": "uncertain", "applied": applied, "message": "?"}
            return ExecutionResult(False, "uncertain", "unknown", eid)
        self.records[eid] = {"status": "completed", "outcome": what, "applied": applied,
                             "verification": {"outcome": what, "reason": what, "criteria": []}}
        return ExecutionResult(True, what, what, eid)


def _values(change):
    if change.action_type == "adjust_resource_limit":
        return f"{change.expected_bytes / 2**20:g}Mi", f"{change.target_bytes / 2**20:g}Mi"
    return str(change.expected_replicas), str(change.target_replicas)


def build(tmp_path, world, script, supplied_first=None):
    plan, _ = make_plan(world)
    review = ReviewService(tmp_path / "r.db", [HUMAN, AGENT_ID])
    digest = review.register_plan("INC-A", plan, time.time())
    events = []
    ex = FakeExecution(review, script)
    store = AutoStore(tmp_path / "a.db")
    agent = RemediationAgent(review, ex, store, lambda t, d: events.append((t, d)), log=lambda *_: None)
    return agent, review, ex, store, events, {"id": "INC-A", "digest": digest}


def kinds(events):
    return [t for t, _ in events]


# --------------------------------------------------------------------------- gate + ladder

def test_the_gate_passes_only_when_every_check_holds():
    pol = AutoPolicy.load(POLICY)
    route, ai, p = good_inputs()
    assert gate(pol, True, route, ai, p, {})["eligible"]
    assert not gate(pol, False, route, ai, p, {})["eligible"]                                   # switched off
    assert not gate(pol, True, {**route, "mode": "lead"}, ai, p, {})["eligible"]                # agent led
    assert not gate(pol, True, {**route, "rule_confidence": 0.85}, ai, p, {})["eligible"]       # below 90%
    bad = {"status": "ok", "report": {**ai["report"], "agreement": {"category": True, "component": False}}}
    assert not gate(pol, True, route, bad, p, {})["eligible"]                                   # disagreement
    weak = {"status": "ok", "report": {**ai["report"], "validation": {"ok": True, "findings": 4, "supported_findings": 3}}}
    assert not gate(pol, True, route, weak, p, {})["eligible"]                                  # unsupported finding
    assert not gate(pol, True, route, {"status": "error"}, p, {})["eligible"]                   # no agent report
    _, _, cfg = good_inputs("restore_configuration")
    assert not gate(pol, True, route, ai, cfg, {})["eligible"]                                  # needs a human
    assert not gate(pol, True, route, ai, {**p, "parameter": {"value": None}}, {})["eligible"]  # no value
    g = gate(pol, True, route, ai, p, {"backend": {"incident_id": "INC-OLD"}})
    assert not g["eligible"] and "INC-OLD" in [c for c in g["checks"] if not c["ok"]][0]["detail"]   # locked


def test_the_ladder_is_bounded_by_policy():
    pol = AutoPolicy.load(POLICY)
    assert ladder(pol, good_inputs(value="256Mi")[2], GiB) == ["256Mi", "384Mi", "576Mi"]
    assert ladder(pol, good_inputs(value="512Mi")[2], GiB) == ["512Mi", "768Mi"]               # capped at 1Gi
    assert ladder(pol, good_inputs("scale_workload", "1")[2], GiB) == [1]                      # one step only
    assert ladder(pol, good_inputs(value=None)[2], GiB) == []


def test_a_retry_plan_is_a_compare_and_set_from_what_the_last_attempt_left():
    plan, _ = make_plan(fc.world_oom())
    p2 = retry_plan(plan, 0, "256Mi", "384Mi", "INC-A", 2)
    change, _, msg = ExecutionService.change_for(p2, 0, {"supplied_parameters": {}})
    assert change.expected_bytes == 256 * 2**20 and change.target_bytes == 384 * 2**20, msg
    assert p2["incident_id"] == "INC-A#auto-2" and len(p2["actions"]) == 1


# --------------------------------------------------------------------------- runs

def test_resolved_on_the_first_attempt_changes_nothing_else(tmp_path):
    agent, review, ex, store, ev, inc = build(tmp_path, fc.world_oom(), ["RESOLVED"])
    out = agent.run(inc, good_inputs()[2], ["256Mi", "384Mi", "576Mi"], {"checks": []})
    assert out["result"] == "resolved" and len(ex.calls) == 1 and store.locked() == {}
    assert review.effective_decision("INC-A", inc["digest"], 0)["reviewer"] == AGENT_ID   # audited as the agent


def test_not_resolved_climbs_the_ladder_then_resolves(tmp_path):
    agent, review, ex, store, ev, inc = build(tmp_path, fc.world_oom(), ["NOT_RESOLVED", "INCONCLUSIVE", "RESOLVED"])
    out = agent.run(inc, good_inputs()[2], ["256Mi", "384Mi", "576Mi"], {"checks": []})
    assert out["result"] == "resolved" and out["resolved_by"]["n"] == 3
    assert [(c.expected_bytes / 2**20, c.target_bytes / 2**20) for _, c in ex.calls] == [(192, 256), (256, 384),
                                                                                         (384, 576)]
    assert "auto_revert" not in kinds(ev)


def test_an_exhausted_ladder_reverts_to_the_original_value_locks_and_hands_over(tmp_path):
    agent, review, ex, store, ev, inc = build(tmp_path, fc.world_oom(), ["NOT_RESOLVED"] * 3 + ["NOT_RESOLVED"])
    out = agent.run(inc, good_inputs()[2], ["256Mi", "384Mi", "576Mi"], {"checks": []})
    assert out["result"] == "handed_over" and len(out["attempts"]) == 3
    key, revert = ex.calls[-1]
    assert key == "INC-A" + REVERT_KEY and (revert.expected_bytes, revert.target_bytes) == (576 * 2**20, 192 * 2**20)
    assert out["revert"]["ok"] and "backend" in store.locked()
    assert store.acknowledge("INC-A", HUMAN) == ["backend"] and store.locked() == {}


def test_a_refused_attempt_stops_without_retry_and_reverts_what_was_changed(tmp_path):
    agent, review, ex, store, ev, inc = build(tmp_path, fc.world_oom(), ["NOT_RESOLVED", "refused", "NOT_RESOLVED"])
    out = agent.run(inc, good_inputs()[2], ["256Mi", "384Mi", "576Mi"], {"checks": []})
    assert len(out["attempts"]) == 2 and "stopped before a verified change" in out["reason"]
    assert ex.calls[-1][0].endswith(REVERT_KEY) and ex.calls[-1][1].target_bytes == 192 * 2**20


def test_an_uncertain_state_is_never_retried_or_reverted(tmp_path):
    agent, review, ex, store, ev, inc = build(tmp_path, fc.world_oom(), ["uncertain"])
    out = agent.run(inc, good_inputs()[2], ["256Mi", "384Mi"], {"checks": []})
    assert len(ex.calls) == 1 and out["revert"] is None and "uncertain" in out["reason"]
    assert "backend" in store.locked()


def test_stop_ends_attempts_without_a_revert_and_the_stopper_owns_it(tmp_path):
    agent, review, ex, store, ev, inc = build(tmp_path, fc.world_oom(), ["NOT_RESOLVED", "NOT_RESOLVED"])
    original = ex.execute

    def execute(req, progress=None):
        res = original(req, progress)
        agent.stop("INC-A", HUMAN)            # pressed while attempt 1 is running
        return res
    ex.execute = execute
    out = agent.run(inc, good_inputs()[2], ["256Mi", "384Mi"], {"checks": []})
    assert len(ex.calls) == 1 and out["revert"] is None and out["stopped_by"] == HUMAN
    assert store.locked() == {} and "auto_stop" in kinds(ev)


def test_scale_is_a_single_attempt_and_reverts_to_zero(tmp_path):
    agent, review, ex, store, ev, inc = build(tmp_path, fc.world_db_down(), ["NOT_RESOLVED", "INCONCLUSIVE"])
    _, _, p = good_inputs("scale_workload", "1")
    out = agent.run(inc, p, [1], {"checks": []})
    assert [(c.expected_replicas, c.target_replicas) for _, c in ex.calls] == [(0, 1), (1, 0)]
    assert out["result"] == "handed_over"
