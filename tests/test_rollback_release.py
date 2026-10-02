"""rollback_release (demo repo): planned only from recorded history, compare-and-set on the definition's image,
rechecked live, and the agent's proposal never supplies the image itself."""
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fake_cluster as fc  # noqa: E402
from test_slack_review import make_plan  # noqa: E402

from demo import agent_plan  # noqa: E402
from investigator.capabilities import TimeRange  # noqa: E402
from investigator.capabilities.base import ConfigChange  # noqa: E402
from investigator.evidence import EvidenceStore  # noqa: E402
from investigator.execution_model import ChangeRequest, Refusal  # noqa: E402
from investigator.executor import ExecutionService  # noqa: E402
from investigator.recheck import recheck  # noqa: E402
from investigator.remediation import Situation, _rollback_release, plan_rollback  # noqa: E402

BAD, GOOD = "incident-demo/app:0.3", "incident-demo/app:0.2"


def store(pulling=True, history=True):
    s = EvidenceStore()
    if pulling:
        s.add("k8s.resource_state", "component/backend", "process_waiting",
              "Container backend in pod backend-x is waiting: ImagePullBackOff", instance="backend-x",
              process="backend", reason="ImagePullBackOff", cause="image_unavailable", problematic=True)
    if history:
        s.add("k8s.history", "component/backend", "configuration_change",
              f"Deployment/backend: containers[backend].image changed from {GOOD} to {BAD}", component="backend",
              item="containers[backend].image", before=GOOD, after=BAD, sensitive=False, t=100.0)
    return s


def situation(st):
    return Situation({"affected_component": "backend", "category": "image_pull_failure"}, {}, {}, st)


def test_the_rule_plans_a_rollback_to_the_recorded_previous_image():
    a = _rollback_release(situation(store()))
    assert a.type.value == "rollback_release" and a.parameters == {"current_image": BAD, "previous_image": GOOD}
    assert a.parameters_complete and a.target == {"component": "backend", "process": "backend"}
    assert _rollback_release(situation(store(history=False))) is None        # no recorded previous image
    assert _rollback_release(situation(store(pulling=False))) is None        # the release is not failing


def test_it_maps_to_one_typed_change_and_has_its_own_undo():
    from dataclasses import asdict
    from investigator.remediation_model import _plain
    plan, _ = make_plan(fc.world_crash())
    plan["actions"] = [_plain(asdict(_rollback_release(situation(store()))))]
    change, _, msg = ExecutionService.change_for(plan, 0, {"supplied_parameters": {}})
    assert (change.expected_image, change.target_image, change.process) == (BAD, GOOD, "backend"), msg
    undo = plan_rollback(plan, 0, {"before": BAD, "after": GOOD}, "NOT_RESOLVED", "r", "INC#rollback-1")
    assert undo.actions[0].parameters == {"current_image": GOOD, "previous_image": BAD}


def caps(image=BAD, pulling=True, ready=1):
    proc = SimpleNamespace(name="backend", waiting_cause="image_unavailable" if pulling else None)
    st = SimpleNamespace(kind="Deployment", desired=2, ready=ready, images={"backend": image},
                         instances=[SimpleNamespace(ready=not pulling, processes=[proc])])
    return SimpleNamespace(get_resource_state=lambda c, tr: st, list_components=lambda: ["backend"])


def test_the_recheck_is_compare_and_set_on_the_live_image():
    c = ChangeRequest("rollback_release", "backend", process="backend", expected_image=BAD, target_image=GOOD)
    ok = recheck(caps(), c, time.time(), 900)
    assert ok.ok and ok.write == {"component": "backend", "process": "backend", "expected_image": BAD, "new_image": GOOD}
    assert recheck(caps(image=GOOD), c, time.time(), 900).code == Refusal.NOT_NEEDED          # already rolled back
    assert recheck(caps(image="x:9"), c, time.time(), 900).code == Refusal.STATE_CHANGED      # someone changed it
    assert recheck(caps(pulling=False, ready=2), c, time.time(), 900).code == Refusal.NOT_NEEDED   # healthy now


def test_the_agents_rollback_takes_the_image_from_history_not_from_the_model():
    hist = [ConfigChange("backend", "Deployment/backend", "containers[backend].image", GOOD, BAD, False, 50.0, None, None)]
    c = caps()
    c.get_configuration_history = lambda comp, tr: hist
    rep = {"root_cause": "image cannot be pulled", "root_cause_component": "backend",
           "suggested_fix": {"action": "rollback_release", "target": "backend", "parameter_value": "evil/image:1"},
           "validation": {"ok": True, "findings": 2, "supported_findings": 2}}
    plan, why = agent_plan.build(make_plan(fc.world_crash())[0], rep, c, ["backend"], time.time(), "frontend", 3, 2**30)
    assert plan["actions"][0]["parameters"] == {"current_image": BAD, "previous_image": GOOD}, why
    c.get_configuration_history = lambda comp, tr: []
    assert agent_plan.build(make_plan(fc.world_crash())[0], rep, c, ["backend"], time.time(), None, 3, 2**30)[0] is None
