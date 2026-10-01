"""Iteration 7, Milestone 3: verification of an executed change, outcomes, failure handling (no automatic follow-up),
rollback as a separately approved typed plan, and the complete audit chain. Fakes only."""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fake_cluster as fc  # noqa: E402
from test_execution import APPROVER, EXECUTOR, Clock, make_service, req  # noqa: E402
from test_execution_apply import Actuator, with_limit  # noqa: E402

from investigator.capabilities import Capabilities  # noqa: E402
from investigator.capabilities.base import (InstanceState, MetricSeries, ProcessState, RequestResult,  # noqa: E402
                                            ResourceState, ServiceHealth, Termination)
from investigator.capabilities.kubernetes import KubernetesAdapter  # noqa: E402
from investigator.evidence import EvidenceStore  # noqa: E402
from investigator.execution_model import ExecutionRequest, Outcome, Refusal  # noqa: E402
from investigator.execution_store import ExecutionStore  # noqa: E402
from investigator.verification import Verifier  # noqa: E402

NOW = fc.NOW
APPLIED = NOW


# --------------------------------------------------------------------------- scripted evidence for the verifier

class Caps:
    """Answers capability calls from functions of the (fake) time - what the system looks like after a change."""

    def __init__(self, clock, ready_from=0.0, endpoints=1, entry_status=200, oom_at=None, logs=None, ratio=0.01,
                 metrics=True):
        self.clock, self.ready_from, self.endpoints, self.entry_status = clock, ready_from, endpoints, entry_status
        self.oom_at, self.logs, self.ratio, self.metrics = oom_at, logs or [], ratio, metrics

    def __call__(self):
        return self

    def get_resource_state(self, comp, tr):
        t = self.clock()
        ready = t >= APPLIED + self.ready_from
        term = None
        if self.oom_at is not None and t >= APPLIED + self.oom_at:
            term = Termination("OOMKilled", 137, APPLIED + 5, APPLIED + self.oom_at, "memory_limit")
        inst = InstanceState(f"{comp}-new", "Pod", "Running", ready and term is None, None, False, APPLIED + 2,
                             [ProcessState(comp, "running", last_termination=term)])
        old = InstanceState(f"{comp}-old", "Pod", "Running", True, None, False, NOW - 3600,
                            [ProcessState(comp, "running", last_termination=Termination(
                                "OOMKilled", 137, NOW - 90, APPLIED + 1, "memory_limit"))])   # dies during rollout
        return ResourceState(comp, "Deployment", "shop", 1, 1 if inst.ready else 0, 1, {comp: {"memory": "512Mi"}},
                             [inst] + ([old] if t < APPLIED + 20 else []))

    def get_service_health(self, host, port, dep_type):
        return ServiceHealth(host, port, True, True, host, "shop", ready_endpoints=self.endpoints)

    def probe_request(self, service, port, path):
        return RequestResult(self.entry_status, "")

    def get_logs(self, comp, inst, proc, tr):
        return [(t, l) for t, l in self.logs]

    def get_metrics(self, metric, tr, target=None, component=None):
        if not self.metrics:
            return None
        pts = [(tr.start + i * 5, self.ratio) for i in range(int((tr.end - tr.start) / 5) + 1)]
        return [MetricSeries({"window_s": 15}, pts)]


def crit(check, subject, **exp):
    return {"check": check, "subject": subject, "expectation": exp, "statement": f"{check} {subject}"}


OOM_CRITERIA = [crit("component_ready", "backend", ready_equals_desired=True),
                crit("no_new_terminations", "backend", cause="memory_limit", count=0),
                crit("entry_requests_succeed", "frontend", status_below=500),
                crit("error_ratio_below", "frontend", ratio=0.05)]


def verify(caps, criteria=OOM_CRITERIA, window=120, settle=60):
    clock = caps.clock
    return Verifier(caps, clock, clock.sleep, 5, ("frontend", "8080", "/")).run(criteria, APPLIED, settle, window)


def test_all_criteria_holding_over_the_window_is_resolved():
    clock = Clock(APPLIED)
    v = verify(Caps(clock, ready_from=25))
    assert v.outcome == Outcome.RESOLVED and all(c.status == "pass" for c in v.criteria)
    assert v.settle["ready"] and v.settle["ended_at"] - APPLIED >= 25          # waited for the rollout
    assert v.window["ended_at"] - v.window["started_at"] >= 120
    nt = next(c for c in v.criteria if c.check == "no_new_terminations")
    assert nt.samples > 20 and "0 termination(s)" in nt.evidence[0]         # the old pod's kill is not counted


def test_a_new_kill_after_the_change_is_not_resolved():
    clock = Clock(APPLIED)
    v = verify(Caps(clock, ready_from=10, oom_at=90))
    assert v.outcome == Outcome.NOT_RESOLVED
    failed = {c.check for c in v.criteria if c.status == "fail"}
    assert "no_new_terminations" in failed and "component_ready" in failed
    assert "no_new_terminations backend" in v.reason


def test_a_component_that_never_becomes_ready_is_not_resolved():
    clock = Clock(APPLIED)
    v = verify(Caps(clock, ready_from=10_000))
    assert v.outcome == Outcome.NOT_RESOLVED and not v.settle["ready"]
    assert v.settle["ended_at"] - APPLIED >= 60                              # bounded settle, then judged


def test_failing_user_requests_or_error_ratio_are_not_resolved():
    assert verify(Caps(Clock(APPLIED), entry_status=503)).outcome == Outcome.NOT_RESOLVED
    v = verify(Caps(Clock(APPLIED), ratio=0.2))
    assert v.outcome == Outcome.NOT_RESOLVED and "highest error ratio 20.0%" in \
        next(c for c in v.criteria if c.check == "error_ratio_below").evidence[0]


def test_missing_evidence_is_inconclusive_never_success():
    v = verify(Caps(Clock(APPLIED), metrics=False))
    assert v.outcome == Outcome.INCONCLUSIVE and "could not be established" in v.reason
    assert verify(Caps(Clock(APPLIED)), criteria=[]).outcome == Outcome.INCONCLUSIVE
    v = verify(Caps(Clock(APPLIED)), criteria=[crit("something_new", "x")])
    assert v.outcome == Outcome.INCONCLUSIVE


def test_dependency_criteria_use_logs_after_settling_and_endpoints():
    err = json.dumps({"level": "error", "msg": "database connection failed", "db_host": "postgres", "db_port": "5432",
                      "error": "Connection refused"})
    dep = [crit("component_ready", "postgres", ready_equals_desired=True),      # as in a real scale/restore plan
           crit("dependency_available", "postgres:5432", ready_endpoints_min=1),
           crit("no_dependency_errors", "backend", endpoint="postgres:5432", count=0)]
    before = verify(Caps(Clock(APPLIED), ready_from=10, logs=[(APPLIED + 1, err)]), dep)   # while settling
    assert before.outcome == Outcome.RESOLVED
    during = verify(Caps(Clock(APPLIED), ready_from=10, logs=[(APPLIED + 100, err)]), dep)
    assert during.outcome == Outcome.NOT_RESOLVED
    assert verify(Caps(Clock(APPLIED), endpoints=0), dep).outcome == Outcome.NOT_RESOLVED


# --------------------------------------------------------------------------- the executor end to end

class World:
    """Reads come from `current`; the fake actuator's real write switches it to `after` (what the change caused)."""

    def __init__(self, before, after):
        self.current, self.after = before, after

    def caps(self):
        return Capabilities(KubernetesAdapter(fc.FakeKube(self.current), "shop"), EvidenceStore())


class SwitchingActuator(Actuator):
    def __init__(self, world, **kw):
        super().__init__(**kw)
        self.world = world

    def _r(self, op, dry_run, **kw):
        res = super()._r(op, dry_run, **kw)
        if not dry_run and res.accepted:
            self.world.current = self.world.after
            res.before, res.after = {"set_memory_limit": ("192Mi", kw.get("after") or "256Mi")}.get(op, (res.before,
                                                                                                         res.after))
        return res


def oom_still_failing() -> dict:
    """After the change: new instances, killed again at their memory limit."""
    w = with_limit(fc.world_oom(), "256Mi")
    for p in w["pods"][1:3]:
        p["created"] = NOW + 2
        p["containers"][0]["last_state"]["finished_at"] = NOW + 30
    return w


def run(tmp_path, after, approve=("256Mi",)):
    svc, review, d, plan, _, clock = make_service(tmp_path, world=fc.world_oom(), approve=approve)
    world = World(fc.world_oom(), after)
    svc.actuator, svc.capabilities = SwitchingActuator(world), world.caps
    stages = []
    out = svc.execute(req(d), progress=lambda stage, data: stages.append(stage))
    return svc, review, d, out, stages


def test_successful_remediation_is_resolved_and_offers_no_rollback(tmp_path):
    svc, review, d, out, stages = run(tmp_path, with_limit(fc.base_world(), "256Mi"))
    assert out.ok and out.code == "RESOLVED"
    assert stages == ["checks_passed", "applied", "verifying", "observing", "completed"]
    rec = svc.store.for_action("INC-TEST", d, 0)
    assert rec["status"] == "completed" and rec["outcome"] == "RESOLVED" and "rollback" not in rec["verification"]
    assert [dry for _, dry, _ in svc.actuator.calls] == [True, False]


def test_failed_verification_stops_and_only_offers_a_rollback_plan(tmp_path):
    svc, review, d, out, _ = run(tmp_path, oom_still_failing())
    assert out.code == "NOT_RESOLVED"
    assert [dry for _, dry, _ in svc.actuator.calls] == [True, False]          # no automatic second change
    rb = svc.store.for_action("INC-TEST", d, 0)["verification"]["rollback"]
    assert rb["available"] and rb["incident_key"].startswith("INC-TEST#rollback-")
    rplan = review.plan(rb["incident_key"], rb["digest"])["plan"]
    [action] = rplan["actions"]
    assert action["type"] == "adjust_resource_limit" and action["parameters_complete"] is True
    assert (action["parameters"]["current_limit"], action["parameters"]["proposed_limit"]) == ("256Mi", "192Mi")
    assert rplan["requires_human_approval"] is True and rplan["execution"]["status"] == "not_executed"
    assert review.statuses(rb["incident_key"], rb["digest"])[0]["status"] == "awaiting_review"


def test_a_rollback_needs_its_own_approval_and_goes_through_the_same_controls(tmp_path):
    svc, review, d, out, _ = run(tmp_path, oom_still_failing())
    rb = svc.store.for_action("INC-TEST", d, 0)["verification"]["rollback"]
    rreq = ExecutionRequest(rb["incident_key"], rb["digest"], 0, EXECUTOR, NOW)
    assert svc.execute(rreq).code == Refusal.NOT_APPROVED.value                 # not without a human approval
    assert review.decide(rb["incident_key"], rb["digest"], 0, "approved", APPROVER).effective
    svc.actuator.world.after = with_limit(fc.world_oom(), "192Mi")
    calls = len(svc.actuator.calls)
    back = svc.execute(rreq)
    assert back.ok and back.code in ("RESOLVED", "NOT_RESOLVED", "INCONCLUSIVE")
    ops = svc.actuator.calls[calls:]
    assert [(dry, kw["new"]) for _, dry, kw in ops] == [(True, 192 * 2**20), (False, 192 * 2**20)]   # dry run first
    rec = svc.store.for_action(rb["incident_key"], rb["digest"], 0)
    assert rec["kind"] == "rollback" and "rollback" not in (rec["verification"] or {})   # never a rollback chain
    assert svc.execute(rreq).code == Refusal.ALREADY_COMPLETED.value


def test_the_complete_audit_chain_is_persisted(tmp_path):
    svc, review, d, out, _ = run(tmp_path, with_limit(fc.base_world(), "256Mi"))
    store = ExecutionStore(tmp_path / "reviews.db")                        # read back from disk
    [rec] = store.executions("INC-TEST")
    plan = review.plan("INC-TEST", rec["plan_digest"])["plan"]                      # incident -> diagnosis -> plan
    assert plan["diagnosis"]["category"] == "memory_exhaustion" and rec["plan_digest"] == d
    decision = next(x for x in review.decisions("INC-TEST") if x["id"] == rec["decision_id"])
    assert (decision["decision"], decision["reviewer"], decision["supplied_parameters"]) == \
        ("approved", APPROVER, {"proposed_limit": "256Mi"})
    assert (rec["approver"], rec["executor"], rec["action_index"], rec["action_type"]) == \
        (APPROVER, EXECUTOR, 0, "adjust_resource_limit")
    assert rec["policy"]["allowed"] and rec["recheck"]["ok"] and rec["dry_run"]["accepted"]
    assert rec["applied"]["accepted"] and rec["applied"]["at"] and rec["change"]["target_bytes"] == 256 * 2**20
    v = rec["verification"]
    assert v["outcome"] == rec["outcome"] == "RESOLVED" and v["criteria"] and all(c["evidence"] for c in v["criteria"])
    assert [a["code"] for a in store.attempts("INC-TEST")] == ["claimed"]


def test_an_interrupted_verification_is_recorded_as_inconclusive(tmp_path, monkeypatch):
    from investigator import executor as ex

    def crash(*a, **k):
        raise SystemExit("killed during verification")
    monkeypatch.setattr(ex.Verifier, "run", crash)
    svc, review, d, plan, _, _ = make_service(tmp_path)
    world = World(fc.world_oom(), with_limit(fc.base_world(), "256Mi"))
    svc.actuator, svc.capabilities = SwitchingActuator(world), world.caps
    try:
        svc.execute(req(d))
    except SystemExit:
        pass
    [rec] = ExecutionStore(tmp_path / "reviews.db").recover_interrupted()
    assert rec["status"] == "completed" and rec["outcome"] == "INCONCLUSIVE" and "interrupted" in rec["message"]
    assert svc.execute(req(d)).code == Refusal.ALREADY_COMPLETED.value


def test_a_verifier_error_is_inconclusive_not_success(tmp_path, monkeypatch):
    from investigator import executor as ex

    def broken(*a, **k):
        raise RuntimeError("metrics backend gone")
    monkeypatch.setattr(ex.Verifier, "run", broken)
    svc, review, d, out, _ = run(tmp_path, with_limit(fc.base_world(), "256Mi"))
    assert out.code == "INCONCLUSIVE" and "could not complete" in out.message
    assert svc.store.for_action("INC-TEST", d, 0)["verification"]["rollback"]["available"]


def test_rollback_plans_for_each_action_type():
    from investigator.remediation import plan_rollback
    from test_slack_review import make_plan
    plan, _ = make_plan(fc.world_db_down())
    rp = plan_rollback(plan, 0, {"before": "0", "after": "1"}, "NOT_RESOLVED", "x", "K").to_dict()
    assert rp["actions"][0]["parameters"] == {"current_replicas": 1, "target_replicas": 0}
    assert rp["actions"][0]["verification"] == []                       # nothing to be ready at 0 replicas
    plan, _ = make_plan(fc.world_db_misconfig())
    rp = plan_rollback(plan, 0, {"before": "postgres-wrong", "after": "postgres"}, "INCONCLUSIVE", "x", "K").to_dict()
    assert rp["actions"][0]["parameters"]["restore_to_host"] == "postgres-wrong"
    assert plan_rollback(plan, 0, {"before": None, "after": "postgres"}, "NOT_RESOLVED", "x", "K") is None
    assert plan_rollback(plan, 1, {"before": "a", "after": "b"}, "NOT_RESOLVED", "x", "K") is None  # investigate step
