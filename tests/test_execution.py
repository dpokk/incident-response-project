"""Iteration 7: execution of approved remediation - authorization, plan validity, freshness, policy, typed changes,
idempotency and the audit record. Uses fakes only; nothing here can reach a cluster."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fake_cluster as fc  # noqa: E402
from test_slack_review import APPROVER, OTHER, make_plan  # noqa: E402

from investigator.actuators.base import Actuator, ChangeResult  # noqa: E402
from investigator.execution_model import ExecutionRequest, Refusal, Status, memory_bytes  # noqa: E402
from investigator.execution_policy import ExecutionPolicy  # noqa: E402
from investigator.execution_store import ExecutionStore  # noqa: E402
from investigator.executor import ExecutionService  # noqa: E402
from investigator.review import ReviewService  # noqa: E402

NOW = fc.NOW
POLICY_FILE = Path(__file__).resolve().parent.parent / "config" / "execution_policy.json"
EXECUTOR = "U0EXECUTOR"


class FakeActuator(Actuator):
    name, scope = "fake", "shop"

    def __init__(self):
        self.calls = []

    def _r(self, op, dry_run, **kw):
        self.calls.append((op, dry_run, kw))
        return ChangeResult(True, dry_run, op, "fake")

    def set_memory_limit(self, component, process, expected_bytes, new_bytes, dry_run):
        return self._r("set_memory_limit", dry_run, component=component, new=new_bytes)

    def set_replicas(self, component, expected, new, dry_run):
        return self._r("set_replicas", dry_run, component=component, new=new)

    def set_config_value(self, source, item, expected_value, new_value, restart_component, dry_run):
        return self._r("set_config_value", dry_run, item=item, new=new_value)


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def make_service(tmp_path, world=None, approve=("256Mi",), policy=None, clock=None):
    """A registered, approved plan (action 0) and an execution service with a fake actuator."""
    clock = clock or Clock()
    plan, _ = make_plan(world or fc.world_oom())
    review = ReviewService(tmp_path / "reviews.db", [APPROVER, EXECUTOR], clock=clock)
    digest = review.register_plan("INC-TEST", plan, NOW - 60)
    if approve is not None:
        out = review.decide("INC-TEST", digest, 0, "approved", APPROVER, supplied=approve[0] if approve else None)
        assert out.effective, out.message
    act = FakeActuator()
    svc = ExecutionService(review, ExecutionStore(tmp_path / "reviews.db", clock=clock),
                           policy or ExecutionPolicy.load(POLICY_FILE), act, clock=clock, log=lambda *_: None)
    return svc, review, digest, plan, act, clock


def req(digest, who=EXECUTOR, index=0, t=NOW):
    return ExecutionRequest("INC-TEST", digest, index, who, t)


# --------------------------------------------------------------------------- policy

def test_the_policy_file_loads_with_the_agreed_defaults():
    p = ExecutionPolicy.load(POLICY_FILE)
    assert p.allowed_action_types == {"adjust_resource_limit", "scale_workload", "restore_configuration"}
    assert p.allowed_namespaces == {"shop"} and p.max_memory_bytes == 2**30 and p.max_replicas == 3
    assert p.max_plan_age_s == 900 and p.executors({APPROVER}) == {APPROVER}
    assert (p.verification.settle_max_s, p.verification.window_s) == (60, 120)
    assert p.verification.bounded(30) == 60 and p.verification.bounded(999) == 300


def test_policy_accepts_the_camel_case_names_and_a_separate_executor_list(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"allowed_action_types": ["AdjustResourceLimit", "ScaleWorkload"],
                             "allowed_namespaces": ["shop"], "allowed_executors": ["U1"]}))
    p = ExecutionPolicy.load(f)
    assert p.allowed_action_types == {"adjust_resource_limit", "scale_workload"}
    assert p.may_execute("U1", {APPROVER}) and not p.may_execute(APPROVER, {APPROVER})
    f.write_text(json.dumps({"verification": {"window_seconds": 900}}))
    with pytest.raises(ValueError):
        ExecutionPolicy.load(f)


# --------------------------------------------------------------------------- authorization and plan validity

def test_an_authorized_executor_passes_every_preflight_check(tmp_path):
    svc, _, d, _, _, _ = make_service(tmp_path)
    refusal, ctx = svc.preflight(req(d))
    assert refusal is None
    assert ctx["change"].target_bytes == 256 * 2**20 and ctx["change"].expected_bytes == 192 * 2**20
    assert {c["check"] for c in ctx["checks"]} >= {"authorization", "approval", "plan_age", "policy:action_type",
                                                   "policy:namespace", "policy:max_memory_limit"}


def test_unauthorized_executors_are_refused_and_audited(tmp_path):
    svc, _, d, _, act, _ = make_service(tmp_path)
    out = svc.execute(req(d, who=OTHER))
    assert out.code == Refusal.UNAUTHORIZED.value and not out.ok and act.calls == []
    [a] = svc.store.attempts("INC-TEST")
    assert (a["executor"], a["code"]) == (OTHER, Refusal.UNAUTHORIZED.value)
    assert svc.store.for_action("INC-TEST", d, 0) is None          # a refused request does not burn the action


def test_execution_needs_an_effective_approval_of_that_action(tmp_path):
    svc, review, d, _, _, _ = make_service(tmp_path, approve=None)
    assert svc.preflight(req(d))[0].code == Refusal.NOT_APPROVED.value
    review.decide("INC-TEST", d, 0, "rejected", APPROVER)
    out = svc.preflight(req(d))[0]
    assert out.code == Refusal.NOT_APPROVED.value and "rejected" in out.message
    assert svc.preflight(req(d, index=1))[0].code == Refusal.NOT_APPROVED.value   # investigate_further: acknowledged only


def test_the_exact_current_plan_digest_is_required(tmp_path):
    svc, review, d, plan, _, _ = make_service(tmp_path)
    assert svc.preflight(req("0" * 64))[0].code == Refusal.UNKNOWN_PLAN.value
    newer = json.loads(json.dumps(plan))
    newer["current_state"].append({"statement": "re-investigated", "fact_ids": []})
    new = review.register_plan("INC-TEST", newer, NOW)
    assert svc.preflight(req(d))[0].code == Refusal.SUPERSEDED.value          # the old approval does not carry over
    assert svc.preflight(req(new))[0].code == Refusal.NOT_APPROVED.value     # the new plan is not approved yet


def test_a_plan_older_than_the_policy_maximum_is_refused(tmp_path):
    svc, _, d, _, act, clock = make_service(tmp_path)
    clock.t = NOW - 60 + 899
    assert svc.preflight(req(d))[0] is None
    clock.t = NOW - 60 + 901
    out = svc.execute(req(d))
    assert out.code == Refusal.STALE_PLAN.value and "fresh investigation required" in out.message and act.calls == []


def test_a_plan_without_a_collection_time_is_treated_as_stale(tmp_path):
    svc, review, _, plan, _, _ = make_service(tmp_path, approve=None)
    other = json.loads(json.dumps(plan))
    other["incident_id"] = "x"
    d = review.register_plan("INC-TEST", other, None)
    review.decide("INC-TEST", d, 0, "approved", APPROVER, supplied="256Mi")
    assert svc.preflight(req(d))[0].code == Refusal.STALE_PLAN.value


# --------------------------------------------------------------------------- policy enforcement

def test_policy_limits_are_enforced_independently_of_the_planner(tmp_path):
    svc, _, d, _, act, _ = make_service(tmp_path, approve=("2Gi",))       # review allows up to 64Gi; policy allows 1Gi
    out = svc.execute(req(d))
    assert out.code == Refusal.POLICY.value and "exceeds the maximum 1Gi" in out.message and act.calls == []

    svc, _, d, _, _, _ = make_service(tmp_path / "r", world=fc.world_db_down(), approve=("4",))
    assert "4 replicas exceeds the maximum 3" in svc.preflight(req(d))[0].message
    svc, _, d, _, _, _ = make_service(tmp_path / "r2", world=fc.world_db_down(), approve=("1",))
    assert svc.preflight(req(d))[0] is None


def test_namespace_and_action_type_restrictions(tmp_path):
    p = ExecutionPolicy(allowed_namespaces=frozenset({"elsewhere"}))
    svc, _, d, _, _, _ = make_service(tmp_path, policy=p)
    assert "namespace 'shop' is not allowed" in svc.preflight(req(d))[0].message
    p = ExecutionPolicy(allowed_action_types=frozenset({"scale_workload"}))
    svc, _, d, _, _, _ = make_service(tmp_path / "b", policy=p)
    assert "adjust_resource_limit is not allowed" in svc.preflight(req(d))[0].message


# --------------------------------------------------------------------------- typed changes

def test_each_approved_action_maps_to_exactly_one_typed_change(tmp_path):
    for world, value, expect in (
            (fc.world_oom(), "512Mi", {"action_type": "adjust_resource_limit", "component": "backend",
                                       "process": "backend", "expected_bytes": 192 * 2**20, "target_bytes": 512 * 2**20}),
            (fc.world_db_down(), "1", {"action_type": "scale_workload", "component": "postgres",
                                       "expected_replicas": 0, "target_replicas": 1}),
            (fc.world_db_misconfig(), "postgres", {"action_type": "restore_configuration", "component": "backend",
                                                   "source": "configmap/backend-config", "item": "DATABASE_URL",
                                                   "expected_host": "postgres-wrong", "target_host": "postgres"})):
        svc, _, d, _, _, _ = make_service(tmp_path / expect["action_type"], world=world, approve=(value,))
        refusal, ctx = svc.preflight(req(d))
        assert refusal is None, refusal
        assert ctx["change"].to_dict() == expect


def test_the_executor_never_uses_a_candidate_value(tmp_path):
    plan, _ = make_plan(fc.world_db_misconfig())
    assert plan["actions"][0]["parameters"]["candidate_host"] == "postgres"
    change, code, _ = ExecutionService.change_for(plan, 0, {"supplied_parameters": {}})
    assert change is None and code == Refusal.MISSING_PARAMETER


def test_unsupported_action_types_are_rejected():
    plan, _ = make_plan()
    assert plan["actions"][1]["type"] == "investigate_further"
    assert ExecutionService.change_for(plan, 1, {})[1] == Refusal.UNSUPPORTED_ACTION
    bogus = json.loads(json.dumps(plan))
    bogus["actions"][0]["type"] = "run_command"
    assert ExecutionService.change_for(bogus, 0, {})[1] == Refusal.UNSUPPORTED_ACTION


def test_memory_quantities():
    assert memory_bytes("256Mi") == 256 * 2**20 and memory_bytes("1Gi") == 2**30 and memory_bytes(5) == 5.0
    assert memory_bytes("lots") is None and memory_bytes(None) is None


# --------------------------------------------------------------------------- idempotency and audit identity

def test_a_claim_is_taken_once_and_a_repeat_never_runs_again(tmp_path, monkeypatch):
    svc, _, d, _, _, _ = make_service(tmp_path)
    runs = []

    def run(self, eid, r, ctx):
        runs.append(eid)
        self.store.update(eid, Status.COMPLETED, outcome="RESOLVED")
        return None
    monkeypatch.setattr(ExecutionService, "_run", run)
    svc.execute(req(d))
    again = svc.execute(req(d, t=NOW + 5))
    assert len(runs) == 1 and again.code == Refusal.ALREADY_COMPLETED.value
    rec = svc.store.for_action("INC-TEST", d, 0)
    assert (rec["approver"], rec["executor"], rec["plan_digest"], rec["action_type"]) == \
        (APPROVER, EXECUTOR, d, "adjust_resource_limit")
    assert rec["change"]["target_bytes"] == 256 * 2**20 and rec["policy"]["allowed"] is True
    assert [a["code"] for a in svc.store.attempts("INC-TEST")] == ["claimed", Refusal.ALREADY_COMPLETED.value]


def test_the_claim_itself_is_atomic(tmp_path):
    svc, review, d, _, _, _ = make_service(tmp_path)
    _, ctx = svc.preflight(req(d))
    first = svc.store.claim(req(d), "adjust_resource_limit", ctx["decision"], {}, {})
    second = svc.store.claim(req(d, t=NOW + 1), "adjust_resource_limit", ctx["decision"], {}, {})
    assert first[0] is True and second[0] is False and second[1]["execution_id"] == first[1]["execution_id"]


def test_an_interrupted_execution_becomes_uncertain_and_is_never_retried(tmp_path, monkeypatch):
    svc, _, d, _, _, _ = make_service(tmp_path)

    def crash(self, eid, r, ctx):
        self.store.update(eid, Status.APPLYING)
        raise SystemExit("process killed mid-change")
    monkeypatch.setattr(ExecutionService, "_run", crash)
    with pytest.raises(SystemExit):
        svc.execute(req(d))
    assert svc.execute(req(d)).code == Refusal.IN_PROGRESS.value        # still running as far as anyone knows
    restarted = ExecutionStore(tmp_path / "reviews.db")
    [rec] = restarted.recover_interrupted()
    assert rec["status"] == "uncertain" and "not retried" in rec["message"]
    out = svc.execute(req(d))
    assert out.code == Refusal.UNCERTAIN.value and "fresh investigation" in out.message
