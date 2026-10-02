"""Demo: an agent-led incident whose fix the rule engine could not plan becomes a reviewable, executable plan
(regression: "backend scaled to zero" offered only Acknowledge, so the engineer could not approve the agent's fix)."""
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fake_cluster as fc  # noqa: E402
from test_slack_review import make_plan  # noqa: E402

from demo import agent_plan  # noqa: E402
from investigator.executor import ExecutionService  # noqa: E402
from investigator.review import ReviewService  # noqa: E402

COMPS = ["backend", "frontend", "postgres"]


def caps(desired=0, memory="192Mi"):
    st = SimpleNamespace(desired=desired, limits={"backend": {"memory": memory}})
    return SimpleNamespace(get_resource_state=lambda comp, tr: st)


def report(action="scale_workload", value="2", target="backend", supported=3):
    return {"root_cause": "backend was scaled to 0", "root_cause_component": "backend",
            "suggested_fix": {"action": action, "target": target, "parameter_value": value},
            "validation": {"ok": True, "findings": 3, "supported_findings": supported}}


def undetermined_plan():
    plan, _ = make_plan(fc.world_crash())          # its plan has no typed change
    assert all(a["type"] == "investigate_further" for a in plan["actions"])
    return plan


def test_the_agents_scale_fix_becomes_an_approvable_executable_action(tmp_path):
    plan, why = agent_plan.build(undetermined_plan(), report(), caps(desired=0), COMPS, time.time(), "frontend", 3, 2**30)
    assert plan is not None, why
    a = plan["actions"][0]
    assert a["type"] == "scale_workload" and a["parameters"] == {"current_replicas": 0, "target_replicas": 2}
    assert a["parameters_complete"] and [v["check"] for v in a["verification"]][:2] == ["component_ready",
                                                                                         "entry_requests_succeed"]
    review = ReviewService(tmp_path / "r.db", ["U1"])
    d = review.register_plan("INC-X", plan, time.time())
    out = review.decide("INC-X", d, 0, "approved", "U1")                      # Approve exists now
    assert out.effective, out.message
    change, _, msg = ExecutionService.change_for(plan, 0, out.record)
    assert (change.expected_replicas, change.target_replicas) == (0, 2), msg


def test_memory_fix_reads_the_current_limit_live():
    plan, why = agent_plan.build(undetermined_plan(), report("adjust_resource_limit", "64Mi"), caps(memory="32Mi"),
                                 COMPS, time.time(), "frontend", 3, 2**30)
    p = plan["actions"][0]["parameters"]
    assert (p["current_limit"], p["proposed_limit"]) == ("32Mi", "64Mi"), why


def test_unsafe_or_unproven_suggestions_are_not_turned_into_actions():
    base = undetermined_plan()
    assert agent_plan.build(base, report(supported=2), caps(), COMPS, 0, None, 3, 2**30)[0] is None      # unproven
    assert agent_plan.build(base, report(value="9"), caps(), COMPS, 0, None, 3, 2**30)[0] is None        # > policy
    assert agent_plan.build(base, report(value="0"), caps(desired=0), COMPS, 0, None, 3, 2**30)[0] is None
    assert agent_plan.build(base, report(target="nope"), caps(), ["frontend"], 0, None, 3, 2**30)[0] is None
    assert agent_plan.build(base, report("restore_configuration"), caps(), COMPS, 0, None, 3, 2**30)[0] is None
    assert agent_plan.build(base, report("adjust_resource_limit", "128Mi"), caps(memory="192Mi"), COMPS, 0, None, 3,
                            2**30)[0] is None                                                              # not higher
