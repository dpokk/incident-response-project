"""A reviewable plan from the Investigator Agent's suggested fix, for incidents the rule engine could not plan (demo).

When the rule engine has no established cause, its plan holds no typed change - only "investigate further" - so an
engineer could only acknowledge, even when the agent found a clear, typed fix (e.g. "backend was scaled to 0: scale it
to 2"). This module turns such a suggestion into an ordinary RemediationPlan action, built deterministically:

  * only scale_workload and adjust_resource_limit (a configuration value needs a source the agent cannot prove);
  * only when the agent's report passed validation (every finding supported by evidence it collected);
  * the target must be an existing component; the CURRENT value is read live through capabilities (it becomes the
    executor's compare-and-set precondition), never taken from the model;
  * the proposed value is parsed and bounded by the same rules as an engineer's typed value;
  * verification criteria are the standard ones (component ready, user requests succeed, error ratio below 5%).

The plan replaces the rule engine's plan for the incident (the review store supersedes it), is posted to Slack with
the usual Approve / Reject / Investigate first buttons, and ALWAYS needs a human: the Remediation Agent's gate never
automates an incident the agent led. Nothing here executes anything.
"""
import copy

from investigator.capabilities import TimeRange
from investigator.execution_model import memory_bytes, mib
from investigator.remediation_model import (ActionType, Check, ProposedAction, Rollback, Statement, Urgency,
                                            VerificationCriterion)
from investigator.remediation_model import _plain
from dataclasses import asdict

SUPPORTED = ("scale_workload", "adjust_resource_limit", "rollback_release")
MAX_REPLICAS = 50


def build(rule_plan: dict, ai_report: dict, caps, components: list[str], now: float, entry_app: str | None,
          max_replicas: int, max_memory_bytes: float) -> tuple[dict | None, str]:
    """(plan dict, reason). None when the suggestion cannot become a typed, checked action."""
    fix = (ai_report or {}).get("suggested_fix") or {}
    atype = fix.get("action")
    if atype not in SUPPORTED:
        return None, f"the agent's fix ({atype}) is not a scale, memory or release-rollback change"
    v = ai_report.get("validation") or {}
    if not v.get("ok") or not v.get("findings") or v.get("supported_findings") != v.get("findings"):
        return None, "the agent's report did not pass evidence validation"
    comp = _component(fix.get("target"), ai_report.get("root_cause_component"), components)
    if comp is None:
        return None, f"the agent's target '{fix.get('target')}' is not an existing component"
    if any(a["type"] == atype for a in rule_plan.get("actions", [])):
        return None, "the rule engine's plan already contains this action"
    state = caps.get_resource_state(comp, TimeRange(now - 120, now))
    if state is None:
        return None, f"the live state of {comp} could not be read"
    raw = fix.get("parameter_value")
    why = [Statement(f"Investigator Agent: {ai_report.get('root_cause', '')[:300]}"),
           Statement("The rule engine found no established cause, so its plan had no typed change; this action comes "
                     "from the agent's evidence-checked report and is checked by the same executor as any other.")]
    if atype == "scale_workload":
        try:
            target = int(str(raw).strip())
        except (TypeError, ValueError):
            return None, f"the agent's replica count '{raw}' is not a whole number"
        current = int(state.desired)
        if not 1 <= target <= min(max_replicas, MAX_REPLICAS) or target == current:
            return None, f"replicas {current} -> {target} is outside the allowed range (1-{max_replicas})"
        action = ProposedAction(
            type=ActionType.SCALE_WORKLOAD, target={"component": comp},
            parameters={"current_replicas": current, "target_replicas": target}, parameters_complete=True,
            urgency=Urgency.IMMEDIATE, summary=f"Scale {comp} from {current} to {target} replicas (proposed by the "
                                               f"Investigator Agent)",
            rationale=why, preconditions=[f"{comp} still has {current} desired replica(s)"],
            expected_final_state=[f"{comp} has {target} ready replica(s)"],
            risks=[Statement(f"If {comp} was scaled to {current} on purpose, this undoes that decision")],
            rollback=Rollback("restore_previous_value", f"Scale {comp} back to {current}", {"replicas": current}),
            verification=_verification(comp, entry_app))
    elif atype == "rollback_release":
        images = state.images or {}
        process = comp if comp in images else (next(iter(images)) if len(images) == 1 else None)
        current = images.get(process) if process else None
        if not current:
            return None, f"the current image of {comp} could not be read"
        item = f"containers[{process}].image"
        hist = [c for c in (caps.get_configuration_history(comp, TimeRange(now - 6 * 3600, now)) or [])
                if c.item == item and c.after == current and c.before and c.before != current]
        if not hist:
            return None, f"recorded history has no previous image of {comp} (the model's value is never used)"
        previous = max(hist, key=lambda c: c.t or c.t_latest or 0).before
        action = ProposedAction(
            type=ActionType.ROLLBACK_RELEASE, target={"component": comp, "process": process},
            parameters={"current_image": current, "previous_image": previous}, parameters_complete=True,
            urgency=Urgency.IMMEDIATE, summary=f"Roll back {comp} from {current} to the previous release {previous} "
                                               f"(proposed by the Investigator Agent)",
            rationale=why + [Statement(f"Recorded history: {item} changed from {previous} to {current}")],
            preconditions=[f"{comp} still runs {current}"],
            expected_final_state=[f"{comp} runs {previous} and all instances are ready"],
            risks=[Statement(f"Changes shipped in {current} are withdrawn until it is fixed and released again")],
            rollback=Rollback("restore_previous_value", f"Set the image back to {current}", {"image": current}),
            verification=_verification(comp, entry_app))
    else:
        limits = state.limits or {}
        process = comp if comp in limits else (next(iter(limits)) if len(limits) == 1 else None)
        cur = memory_bytes((limits.get(process) or {}).get("memory")) if process else None
        new = memory_bytes(raw)
        if process is None or cur is None:
            return None, f"the current memory limit of {comp} could not be read"
        if new is None or new <= cur or new > max_memory_bytes:
            return None, (f"memory {mib(cur)} -> {raw} is not an increase within the maximum "
                          f"{mib(max_memory_bytes)}")
        action = ProposedAction(
            type=ActionType.ADJUST_RESOURCE_LIMIT, target={"component": comp, "process": process},
            parameters={"resource": "memory", "current_limit_bytes": cur, "current_limit": mib(cur),
                        "proposed_limit_bytes": new, "proposed_limit": mib(new)}, parameters_complete=True,
            urgency=Urgency.IMMEDIATE, summary=f"Raise {comp}'s memory limit from {mib(cur)} to {mib(new)} "
                                               f"(proposed by the Investigator Agent)",
            rationale=why, preconditions=[f"The live memory limit is still {mib(cur)}"],
            expected_final_state=[f"{comp} runs with {mib(new)} and is not killed at its limit"],
            risks=[Statement("A higher limit uses more node memory; if the cause is a leak, it only delays the kill")],
            rollback=Rollback("restore_previous_value", f"Set the limit back to {mib(cur)}", {"limit_bytes": cur}),
            verification=_verification(comp, entry_app))
    plan = copy.deepcopy(rule_plan)
    plan["actions"] = [_plain(asdict(action))] + [a for a in plan.get("actions", [])
                                                  if a["type"] == "investigate_further"]
    plan["assessment"] = "action_proposed"
    plan["assessment_reason"] = ("the Investigator Agent proposed a typed fix with evidence-checked findings; the rule "
                                 "engine found no established cause")
    plan["generated_by"] = ("Investigator Agent proposal, made into a typed action deterministically (current value "
                            "read live; value bounded; standard verification) - plans only, never executes")
    return plan, "ok"


def _component(target, rcc, components: list[str]) -> str | None:
    for c in (str(target or "").strip(), str(rcc or "").strip()):
        c = c.split("/")[-1]
        if c in components:
            return c
    return None


def _verification(comp: str, entry_app: str | None) -> list[VerificationCriterion]:
    out = [VerificationCriterion(Check.COMPONENT_READY, comp, {"ready_equals_desired": True},
                                 f"All {comp} instances are ready"),
           VerificationCriterion(Check.ENTRY_REQUESTS_SUCCEED, entry_app or comp, {"status_below": 500},
                                 "Synthetic user requests succeed")]
    if entry_app:
        out.append(VerificationCriterion(Check.ERROR_RATIO_BELOW, entry_app, {"ratio": 0.05},
                                         f"The user-facing error ratio at {entry_app} stays below 5%"))
    return out
