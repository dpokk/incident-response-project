"""Remediation planning (Iteration 5): a diagnosis-driven, evidence-backed plan for a human. Never an execution."""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fake_cluster as fc  # noqa: E402
from test_dependency_history import fill_outage, world_db_recovered  # noqa: E402
from test_history import fill_oom_replaced_history, history, investigate, world_oom_pods_replaced  # noqa: E402

from investigator.remediation import plan_remediation  # noqa: E402
from investigator.remediation_model import SCHEMA_VERSION  # noqa: E402
from investigator.report import plan_lines  # noqa: E402

CHANGE_TYPES = {"adjust_resource_limit", "scale_workload", "restore_configuration"}


def plan_for(world, store=None, dx_override=None, rc_override=None):
    dx, facts, report = investigate(world, store)
    dx = {**dx, **(dx_override or {})}
    rc = {**report["reconstruction"], **(rc_override or {})}
    return plan_remediation(dx, rc, report["impact"], facts, "INC-TEST").to_dict(), dx, facts, report


def changes(plan):
    return [a for a in plan["actions"] if a["type"] in CHANGE_TYPES]


def investigate_action(plan):
    return next((a for a in plan["actions"] if a["type"] == "investigate_further"), None)


def assert_actionable_complete(a):
    """Every proposed change carries the full human-review payload."""
    assert a["rationale"] and all(s["statement"] for s in a["rationale"])
    assert a["expected_final_state"] and a["risks"] and a["verification"]
    assert a["rollback"] and a["rollback"]["strategy"] == "restore_previous_value"
    assert any(s["fact_ids"] for s in a["rationale"])                          # tied to evidence


def assert_not_executed(plan):
    assert plan["requires_human_approval"] is True
    assert plan["execution"] == {"status": "not_executed", "executable": False}
    assert plan["approval"] == {"status": "awaiting_review"}


# Test 1 - OOM --------------------------------------------------------------------------------------------------

def test_oom_plan_is_diagnosis_driven_complete_and_not_executed():
    plan, dx, facts, _ = plan_for(fc.world_oom())
    assert plan["diagnosis"]["category"] == dx["category"] == "memory_exhaustion"
    assert plan["incident_state"] == "active" and plan["assessment"] == "action_proposed"
    [a] = changes(plan)
    assert a["type"] == "adjust_resource_limit" and a["urgency"] == "immediate"
    assert a["target"]["component"] == "backend"
    assert a["parameters"]["current_limit"] == "192Mi" and a["parameters"]["proposed_limit_bytes"] is None
    assert a["parameters_complete"] is False                                   # no invented number
    assert_actionable_complete(a)
    assert {v["check"] for v in a["verification"]} >= {"component_ready", "no_new_terminations"}
    assert any("only delays the next kill" in r["statement"] for r in a["risks"])   # not just "add memory"
    assert any("memory grows under load" in q for q in investigate_action(plan)["parameters"]["questions"])
    assert_not_executed(plan)


# Test 2 - recovered PostgreSQL ------------------------------------------------------------------------------------

def test_recovered_postgres_outage_gets_no_blind_restart(tmp_path):
    store, clock = history(tmp_path)
    fill_outage(store, clock)
    plan, dx, facts, _ = plan_for(world_db_recovered(), store)
    assert plan["diagnosis"]["root_cause_component"]["name"] == "postgres"
    assert plan["incident_state"] == "recovered" and plan["assessment"] == "no_immediate_action"
    assert changes(plan) == []                                                 # nothing done to postgres
    states = " ".join(c["statement"] for c in plan["current_state"])
    assert "1 ready" in states and "During the incident (recorded history)" in states
    assert any("no change to it is needed now" in q for q in investigate_action(plan)["parameters"]["questions"])
    json.dumps(plan)


# Test 3 - active PostgreSQL outage --------------------------------------------------------------------------------

def test_active_postgres_outage_proposes_restoring_capacity():
    plan, dx, facts, _ = plan_for(fc.world_db_down())
    assert plan["incident_state"] == "active" and plan["assessment"] == "action_proposed"
    [a] = changes(plan)
    assert a["type"] == "scale_workload" and a["target"] == {"component": "postgres"} and a["urgency"] == "immediate"
    assert a["parameters"] == {"current_replicas": 0, "target_replicas": None} and not a["parameters_complete"]
    assert_actionable_complete(a)
    assert {v["check"] for v in a["verification"]} >= {"dependency_available", "no_dependency_errors", "component_ready"}
    assert any("intentional" in r["statement"] for r in a["risks"])         # the scale-down was a recorded change


# Test 4 - configuration failure ----------------------------------------------------------------------------------

def test_configuration_failure_leads_to_a_configuration_change_not_a_restart():
    plan, dx, facts, _ = plan_for(fc.world_db_misconfig())
    [a] = changes(plan)
    assert a["type"] == "restore_configuration"
    assert a["target"] == {"config_item": "DATABASE_URL", "source": "configmap/backend-config"}
    assert a["parameters"]["current_host"] == "postgres-wrong" and a["parameters"]["candidate_host"] == "postgres"
    assert a["parameters_complete"] is False                                   # a candidate, to be confirmed
    assert_actionable_complete(a)
    assert not any(x["type"] == "scale_workload" or x["target"].get("component") == "postgres"
                   for x in changes(plan))                                     # no unrelated infrastructure change


def test_a_recorded_previous_value_is_restored_exactly(tmp_path):
    """With configuration history, the previous value comes from evidence instead of a candidate."""
    from investigator.capabilities.kubernetes_recorder import ENDPOINTS_KIND  # noqa: F401 - store is provider-fed
    from test_history import record_session
    store, clock = history(tmp_path)
    record_session(store, clock, fc.NOW - 900, fc.NOW)
    clock.t = fc.NOW - 600
    store.add_version("shop", "ConfigMap", "backend-config", None, {"DATABASE_URL": "postgresql://shop@postgres:5432/shop"})
    clock.t = fc.NOW - 200
    store.add_version("shop", "ConfigMap", "backend-config", None,
                      {"DATABASE_URL": "postgresql://shop@postgres-wrong:5432/shop"}, modified_at=fc.NOW - 200)
    plan, *_ = plan_for(fc.world_db_misconfig(), store)
    [a] = changes(plan)
    assert a["parameters"]["restore_to"] == "postgresql://shop@postgres:5432/shop" and a["parameters_complete"]


# Test 5 - application crash -------------------------------------------------------------------------------------

def test_application_crash_plan_follows_the_diagnosis_and_rejects_a_restart():
    plan, dx, facts, _ = plan_for(fc.world_crash())
    assert plan["assessment"] == "no_safe_action" and changes(plan) == []
    qs = investigate_action(plan)["parameters"]["questions"]
    assert any("DivisionByZero" in q and "invoice_batch" in q for q in qs)
    assert any("restart of backend is not proposed: the failure recurs on every start" in q for q in qs)


def test_a_crash_caused_by_a_dependency_is_planned_as_the_dependency():
    plan, dx, facts, _ = plan_for(fc.world_crash_caused_by_db_down())
    assert dx["category"] == "dependency_unavailable"
    assert [(a["type"], a["target"]) for a in changes(plan)] == [("scale_workload", {"component": "postgres"})]


# Test 6 - insufficient evidence ----------------------------------------------------------------------------------

def test_insufficient_evidence_produces_no_change():
    plan, *_ = plan_for(fc.world_healthy_after_rollout())
    assert plan["assessment"] == "investigate_further" and changes(plan) == []
    assert plan["uncertainty"]["confidence"] == 0.0 and plan["incident_state"] == "unknown"


def test_a_low_confidence_diagnosis_is_not_acted_on_even_if_an_action_would_be_eligible():
    plan, *_ = plan_for(fc.world_oom(), dx_override={"confidence": 0.4, "confidence_label": "Low"})
    assert plan["assessment"] == "investigate_further" and changes(plan) == []
    assert "not established strongly enough" in investigate_action(plan)["parameters"]["questions"][0]


def test_competing_causes_are_carried_into_the_plan():
    plan, dx, *_ = plan_for(fc.world_crash_caused_by_db_down())
    assert {(c["category"], c["component"]) for c in plan["uncertainty"]["competing_causes"]} == \
        {("application_crash", "backend")}


# Test 7 - recovered incident ---------------------------------------------------------------------------------------

def test_a_recovered_oom_gets_only_a_preventive_proposal(tmp_path):
    store, clock = history(tmp_path)
    fill_oom_replaced_history(store, clock)
    plan, *_ = plan_for(world_oom_pods_replaced(), store)
    assert plan["incident_state"] == "recovered" and plan["assessment"] == "no_immediate_action"
    assert [(a["type"], a["urgency"]) for a in changes(plan)] == [("adjust_resource_limit", "preventive")]


def test_recovered_is_not_absolute_a_condition_that_holds_now_still_needs_action():
    """The reconstruction says recovered, but the dependency still has zero replicas now: that current condition
    decides, not the label."""
    plan, *_ = plan_for(fc.world_db_down(), rc_override={"window": {"ongoing": False, "incident_end": {"latest": 1}}})
    assert [(a["type"], a["urgency"]) for a in changes(plan)] == [("scale_workload", "immediate")]


# Test 8 - unsupported action ---------------------------------------------------------------------------------------

def test_no_supported_typed_action_is_reported_explicitly():
    plan, *_ = plan_for(fc.world_liveness_kill())
    assert plan["diagnosis"]["category"] == "health_check_failure"
    assert plan["assessment"] == "no_safe_action" and changes(plan) == []
    assert "no supported typed action" in plan["assessment_reason"]
    assert investigate_action(plan)["parameters"]["questions"]


# Design properties -------------------------------------------------------------------------------------------------

def test_actions_follow_evidence_not_the_category_name():
    """Renaming the diagnosis category does not change which actions are eligible."""
    for world in (fc.world_oom, fc.world_db_down, fc.world_db_misconfig, fc.world_crash):
        plan, *_ = plan_for(world())
        renamed, *_ = plan_for(world(), dx_override={"category": "some_new_category", "category_label": "Something new"})
        assert [(a["type"], a["target"], a["urgency"]) for a in changes(plan)] == \
            [(a["type"], a["target"], a["urgency"]) for a in changes(renamed)], world.__name__


def test_planning_is_deterministic_serializable_and_versioned():
    a, *_ = plan_for(fc.world_oom())
    b, *_ = plan_for(fc.world_oom())
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert json.loads(json.dumps(a)) == a and a["schema_version"] == SCHEMA_VERSION
    cited = {f for x in a["actions"] for s in x["rationale"] + x["risks"] for f in s["fact_ids"]}
    assert cited <= {e["id"] for e in a["evidence"]}                           # every citation is in the snapshot
    assert "REMEDIATION PLAN" in "\n".join(plan_lines(a))                      # renders from the dict alone


def test_planning_changes_nothing_in_the_investigation_and_calls_no_capability():
    dx, facts, report = investigate(fc.world_oom())
    before = (copy.deepcopy(dx), copy.deepcopy(report["reconstruction"]), copy.deepcopy(report["impact"]),
              len(facts.facts), len(facts.trace))
    plan_remediation(dx, report["reconstruction"], report["impact"], facts, "X")
    assert (dx, report["reconstruction"], report["impact"], len(facts.facts), len(facts.trace)) == before


def test_actions_are_typed_and_carry_no_command():
    for world in (fc.world_oom, fc.world_db_down, fc.world_db_misconfig, fc.world_crash, fc.world_liveness_kill):
        plan, *_ = plan_for(world())
        for a in plan["actions"]:
            assert a["type"] in CHANGE_TYPES | {"investigate_further"}
            blob = json.dumps(a).lower()
            assert "kubectl" not in blob and "command" not in a["parameters"] and "shell" not in blob
