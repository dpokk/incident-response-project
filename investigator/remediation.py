"""Remediation planning (Iteration 5): from a finished investigation to a structured plan for a human. Plans only.

    diagnosis + reconstruction + impact + evidence  ->  plan_remediation()  ->  RemediationPlan  ->  STOP

Execution boundary (enforced by tests/test_architecture.py): this module receives no capabilities object and no
provider, imports nothing that can reach the system, and has no execution path. It reads the evidence the
investigation already gathered; it does not re-derive the root cause.

How actions are chosen - generically, never by incident name:
  * Evidence predicates describe the situation in neutral terms: terminations at a resource limit, a component
    with zero desired replicas now, a configured endpoint that does not exist now, recorded changes, application
    exceptions, competing causes, the current readiness of each component.
  * Each action type has its own eligibility over those predicates. Its urgency comes from whether the condition
    it addresses holds *now* (IMMEDIATE), held only during the incident (PREVENTIVE), or is moot (not proposed).
    A historical failure therefore never turns into a current action by itself.
  * Corrective actions require a diagnosis that is not Low confidence; otherwise the plan says what to
    investigate. "No safe action" is a valid result; an action is never manufactured.
  * Values the evidence cannot supply (e.g. how much memory is enough) are left empty and flagged for a human.
"""
import re

from .evidence import EvidenceStore, Fact
from .remediation_model import (ActionType, Assessment, Check, IncidentState, ProposedAction, RemediationPlan,
                                Rollback, Statement, Uncertainty, Urgency, VerificationCriterion)

RESOURCE_OF_CAUSE = {"memory_limit": "memory"}     # neutral termination cause -> the resource whose limit was hit
COMPETING_MIN_SCORE = 0.3


def _mib(b) -> str | None:
    return f"{b / 2**20:.0f}Mi" if b else None


# --------------------------------------------------------------------------- evidence predicates (neutral)

class Situation:
    """Read-only view of the investigation, in the terms the action rules need."""

    def __init__(self, dx: dict, reconstruction: dict | None, impact: dict | None, store: EvidenceStore):
        self.dx, self.rc, self.impact, self.store = dx, reconstruction or {}, impact or {}, store
        self.affected = dx.get("affected_component")
        rcc = dx.get("root_cause_component") or {}
        self.root_kind, self.root_name = rcc.get("kind"), rcc.get("name")
        self.root_component = self.root_name if self.root_kind == "component" else None
        self.dependency = (dx.get("dependencies") or [None])[0]

    def find(self, kind, subject=None, **match) -> list[Fact]:
        return self.store.find(kind=kind, subject=subject, **match)

    # current state (as collected) ---------------------------------------------------
    def status(self, comp: str | None) -> Fact | None:
        return next(iter(self.find("component_status", f"component/{comp}")), None) if comp else None

    def ready_now(self, comp: str | None) -> bool | None:
        s = self.status(comp)
        if s is None:
            return None
        insts = self.find("instance_status", f"component/{comp}")
        return s.data.get("desired", 0) > 0 and s.data.get("ready", 0) >= s.data.get("desired", 0) \
            and all(i.data.get("ready") for i in insts)

    def desired_now(self, comp: str | None) -> tuple[int | None, Fact | None]:
        s = self.status(comp)
        if s is not None:
            return s.data.get("desired"), s
        b = next((f for f in self.store.facts if f.kind == "backing_component" and f.data.get("component") == comp), None)
        return (b.data.get("desired"), b) if b else (None, None)

    def failing_now(self, comp: str | None) -> bool:
        """The component itself is failing at collection time: not ready, or stuck/backing off."""
        if not comp:
            return False
        stuck = [w for w in self.find("process_waiting", f"component/{comp}") if w.data.get("problematic")]
        return self.ready_now(comp) is False or bool(stuck)

    def endpoint_missing_now(self) -> Fact | None:
        if not self.dependency:
            return None
        subj = f"dependency/{self.dependency['endpoint']}"
        return next(iter(self.find("service_lookup", subj, found=False)), None) or \
            next(iter(self.find("service_port_mismatch", subj)), None)

    # history -----------------------------------------------------------------------
    def limit_kills(self, comp: str | None) -> list[Fact]:
        return [t for t in self.find("process_terminated", f"component/{comp}")
                if t.data.get("cause") in RESOURCE_OF_CAUSE] if comp else []

    def resource_pressure(self, comp: str | None) -> list[Fact]:
        return [s for s in self.find("log_signature", f"component/{comp}") if s.data.get("signature") == "memory_pressure"] \
            + self.find("metric_memory_high", f"component/{comp}") if comp else []

    def linked_load(self) -> dict | None:
        return next((c for c in self.dx.get("correlations") or [] if c.get("kind") == "traffic_memory"
                     and c.get("linked") is True), None)

    def recorded_changes(self, comp: str | None) -> list[Fact]:
        """Changes recorded on a component: rollouts, configuration changes, scaling/stopping events."""
        if not comp:
            return []
        out = self.find("change", f"component/{comp}") + self.find("configuration_change", f"component/{comp}")
        out += [e for e in self.find("event", f"component/{comp}")
                if e.data.get("category") in ("scaled", "stopped", "instance_deleted")]
        return sorted(out, key=lambda f: f.t or 0)

    def app_exceptions(self, comp: str | None) -> list[Fact]:
        return [e for e in self.find("log_exception", f"component/{comp}") if not e.data.get("dependency_signature")] \
            if comp else []

    def recurs_on_start(self, comp: str | None) -> list[Fact]:
        """Evidence that a restart does not clear the failure: restart back-off, or repeated terminations."""
        if not comp:
            return []
        backoff = [w for w in self.find("process_waiting", f"component/{comp}") if w.data.get("cause") == "restart_backoff"]
        backoff += [e for e in self.find("event", f"component/{comp}") if e.data.get("category") == "restart_backoff"]
        terms = self.find("process_terminated", f"component/{comp}")
        return backoff[:2] + (terms[:2] if len(terms) > 1 else [])

    def competing(self) -> list[dict]:
        primary = (self.dx.get("category"), self.affected)
        return [a for a in self.dx.get("alternatives") or [] if a.get("score", 0) >= COMPETING_MIN_SCORE
                and (a["category"], a["component"]) != primary and not str(a.get("why_not", "")).startswith("explained by")]

    def outage(self) -> Fact | None:
        if not self.dependency:
            return None
        return next(iter(self.find("availability_outage", f"dependency/{self.dependency['endpoint']}")), None)

    def gaps(self) -> list[Fact]:
        return self.find("evidence_gap")

    def entry_component(self) -> str | None:
        err = next(iter(self.find("metric_error_ratio")), None)
        return err.data.get("component") if err else None


# --------------------------------------------------------------------------- action candidates (one rule per type)

def _resource_limit(s: Situation) -> ProposedAction | None:
    """Eligible when the root-cause component was terminated at a resource limit."""
    comp = s.root_component
    kills = s.limit_kills(comp)
    if not kills:
        return None
    k = kills[0]
    resource = RESOURCE_OF_CAUSE[k.data["cause"]]
    current = k.data.get("memory_limit")
    now = s.failing_now(comp)
    pressure = s.resource_pressure(comp)
    observed = next(iter(s.find("metric_memory_observed", f"component/{comp}")), None)
    status = s.status(comp)
    rationale = [Statement(f"{len(kills)} termination(s) of {comp} were kills at its {resource} limit"
                           + (f" ({_mib(current)})" if current else ""), [f.id for f in kills[:4]])]
    if pressure:
        rationale.append(Statement(f"{comp} reported {resource} pressure before the kills", [f.id for f in pressure[:2]]))
    if s.linked_load():
        rationale.append(Statement(s.linked_load()["statement"], s.linked_load()["facts"]))
    rationale.append(Statement(f"{comp} is failing now ({status.text})" if now and status else
                               f"{comp} is not failing at the moment ({status.text if status else 'state unknown'}): "
                               f"this would prevent a recurrence under the same load, it does not fix a current failure",
                               [status.id] if status else []))
    risks = [Statement(f"Reserving more {resource} per instance of {comp} needs spare capacity; whether the "
                       f"cluster has it was not examined")]
    if pressure or s.linked_load():
        risks.insert(0, Statement(f"{comp}'s {resource} grew under load before the kills; if that growth is unbounded "
                                  f"(e.g. an unbounded request backlog), a higher limit only delays the next kill",
                                  [f.id for f in pressure[:2]]))
    else:
        risks.insert(0, Statement(f"The evidence does not show why {comp} needed more {resource}; the limit may "
                                  f"not be the defect"))
    risks.append(Statement(f"Applying a new limit restarts {comp}'s instances (a rollout): brief loss of capacity",
                           [status.id] if status else []))
    sampled = (f"; the highest sampled usage was {observed.data['peak_ratio']:.0%} of the limit, sampled about every "
               f"{observed.data['spacing_s']:.0f}s, so the peak was not measured" if observed and observed.data.get("spacing_s")
               else "")
    return ProposedAction(
        type=ActionType.ADJUST_RESOURCE_LIMIT, target={"component": comp, "process": k.data.get("process")},
        parameters={"resource": resource, "current_limit_bytes": current, "current_limit": _mib(current),
                    "proposed_limit_bytes": None},
        parameters_complete=False, urgency=Urgency.IMMEDIATE if now else Urgency.PREVENTIVE,
        summary=f"Raise {comp}'s {resource} limit (currently {_mib(current) or 'unknown'}); the new value must be "
                f"chosen by an engineer",
        rationale=rationale,
        preconditions=[f"An engineer chooses the new {resource} limit: the evidence does not establish how much "
                       f"{comp} needs{sampled}"],
        expected_final_state=[f"All {comp} instances ready and staying ready",
                              f"No new terminations of {comp} at its {resource} limit under comparable load"],
        risks=risks,
        rollback=Rollback("restore_previous_value", f"Restore the previous {resource} limit"
                          + (f" ({_mib(current)})" if current else ""), {"limit_bytes": current}),
        verification=_verify_component(comp) + [
            VerificationCriterion(Check.NO_NEW_TERMINATIONS, comp, {"cause": k.data["cause"], "count": 0},
                                  f"No new terminations of {comp} at its {resource} limit")]
        + _verify_entry(s))


def _restore_capacity(s: Situation) -> ProposedAction | None:
    """Eligible when the component that failed (as the root cause) has zero desired replicas *now*."""
    comp = s.root_component
    desired, fact = s.desired_now(comp)
    if comp is None or comp == s.affected or desired != 0:
        return None
    changes = s.recorded_changes(comp)
    consumer, ep = s.affected, (s.dependency or {}).get("endpoint")
    rationale = [Statement(f"{comp}, which serves {ep or 'the dependency'}, has 0 desired replicas now", [fact.id])]
    errs = [i for i in s.dx.get("evidence", []) if (f := s.store.by_id(i)) and f.kind == "log_signature"
            and f.subject == f"component/{consumer}"]
    if errs:
        rationale.append(Statement(f"{consumer}'s connection errors for {ep} started when it went away", errs[:2]))
    risks = []
    if changes:
        risks.append(Statement(f"{comp} was scaled down by a recorded change; it may have been intentional (e.g. "
                               f"maintenance): confirm with whoever made it before reversing it",
                               [c.id for c in changes[:2]]))
    crash = next((a for a in s.competing() if a["category"] == "application_crash"), None)
    risks.append(Statement(f"When {comp} returns, its consumers reconnect at once"
                           + (f"; {crash['component']} has also shown a crash in this incident, which may recur "
                              f"on reconnect" if crash else ""), []))
    risks.append(Statement(f"{comp}'s start-up time and state on restart were not examined"))
    return ProposedAction(
        type=ActionType.SCALE_WORKLOAD, target={"component": comp},
        parameters={"current_replicas": 0, "target_replicas": None},
        parameters_complete=False, urgency=Urgency.IMMEDIATE,
        summary=f"Restore {comp}'s replicas (now 0); the previous count is not in the evidence and must be confirmed",
        rationale=rationale,
        preconditions=[f"Confirm the scale-down of {comp} was not intended", f"Confirm the replica count to restore"],
        expected_final_state=[f"{comp} has ready replicas and {ep} has ready endpoints",
                              f"{consumer} connects to {ep} without errors"],
        risks=risks,
        rollback=Rollback("restore_previous_value", f"Scale {comp} back to 0 replicas", {"replicas": 0}),
        verification=[VerificationCriterion(Check.DEPENDENCY_AVAILABLE, ep, {"ready_endpoints_min": 1},
                                            f"{ep} has at least one ready endpoint and accepts connections")]
        + _verify_component(comp)
        + [VerificationCriterion(Check.NO_DEPENDENCY_ERRORS, consumer, {"endpoint": ep, "count": 0},
                                 f"{consumer} logs no new connection errors for {ep}")] + _verify_entry(s))


def _restore_configuration(s: Situation) -> ProposedAction | None:
    """Eligible when the root cause is a configuration item and the fault it caused still holds now."""
    dep = s.dependency
    if s.root_kind != "configuration" or not dep:
        return None
    fault = s.endpoint_missing_now()
    if fault is None:
        return None                                       # the configured endpoint resolves now: moot
    item, source, host = dep["variable"], dep["source"], dep["host"]
    recorded = next((c for c in s.find("configuration_change", f"component/{s.affected}")
                     if c.data.get("item") == item and not c.data.get("sensitive") and c.data.get("before")), None)
    candidates = sorted((f for f in s.find("similar_service", f"dependency/{dep['endpoint']}")
                         if f.data.get("ready", 0) > 0 and f.data.get("port_match")),
                        key=lambda f: -f.data.get("name_similarity", 0))
    change = next(iter(s.find("config_changed")), None)
    if recorded:
        restore_to, complete, how = recorded.data["before"], True, Statement(
            f"Recorded history has the previous value of {item}", [recorded.id])
    elif candidates:
        restore_to, complete, how = None, False, Statement(
            f"A healthy service '{candidates[0].data['service']}' exists on the same port: a candidate for the "
            f"intended host, to be confirmed", [candidates[0].id])
    else:
        restore_to, complete, how = None, False, Statement(f"The intended value is not in the evidence")
    rationale = [Statement(f"{s.affected} is configured ({item} from {source}) to reach '{host}', which does not "
                           f"resolve to a usable service now", [fault.id]), how]
    if change:
        rationale.append(Statement(f"{source} was modified shortly before the failures", [change.id]))
    risks = [Statement(f"If {s.affected} reads {item} only at start-up, the corrected value takes effect after its "
                       f"instances restart (brief loss of capacity)"),
             Statement("A wrong value keeps the dependency unreachable; confirm the target before applying")]
    if change:
        risks.append(Statement(f"{source} was changed recently, perhaps on purpose: confirm with whoever changed it",
                               [change.id]))
    return ProposedAction(
        type=ActionType.RESTORE_CONFIGURATION, target={"config_item": item, "source": source},
        parameters={"current_host": host, "restore_to": restore_to,
                    "candidate_host": candidates[0].data["service"] if candidates else None},
        parameters_complete=complete, urgency=Urgency.IMMEDIATE,
        summary=(f"Restore {item} to its previous value" if recorded else
                 f"Correct the host in {item} ({source}); '{candidates[0].data['service']}' is a candidate"
                 if candidates else f"Correct {item} ({source}); the intended value must be supplied"),
        rationale=rationale,
        preconditions=["Confirm the intended value" if not complete else "Confirm nobody intends the current value"],
        expected_final_state=[f"{s.affected} reaches its {dep['type']} dependency without errors",
                              f"All {s.affected} instances ready"],
        risks=risks,
        rollback=Rollback("restore_previous_value", f"Put back the value being replaced (host '{host}')",
                          {"host": host}),
        verification=[VerificationCriterion(Check.NO_DEPENDENCY_ERRORS, s.affected, {"count": 0},
                                            f"{s.affected} logs no new connection errors for its {dep['type']} "
                                            f"dependency")] + _verify_component(s.affected) + _verify_entry(s))


def _scale_for_load(s: Situation) -> ProposedAction | None:
    """Eligible only when the evidence *links* a load increase to the failure (correlation established)."""
    link, comp = s.linked_load(), s.root_component
    if not link or not comp:
        return None
    desired, fact = s.desired_now(comp)
    return ProposedAction(
        type=ActionType.SCALE_WORKLOAD, target={"component": comp},
        parameters={"current_replicas": desired, "target_replicas": None}, parameters_complete=False,
        urgency=Urgency.IMMEDIATE if s.failing_now(comp) else Urgency.PREVENTIVE,
        summary=f"Add replicas to {comp} to spread the increased load (count to be chosen by an engineer)",
        rationale=[Statement(link["statement"], link["facts"])],
        preconditions=["An engineer chooses the replica count"],
        expected_final_state=[f"{comp} stays ready under the increased load"],
        risks=[Statement("More replicas need cluster capacity, which was not examined"),
               Statement(f"If each instance's memory still grows without bound, more replicas only delay the kills")],
        rollback=Rollback("restore_previous_value", f"Scale {comp} back to {desired} replicas", {"replicas": desired}),
        verification=_verify_component(comp) + _verify_entry(s))


def _investigate(s: Situation, corrective: list[ProposedAction], gated: bool) -> ProposedAction | None:
    """What a human should examine, each point tied to the evidence that raises it."""
    questions: list[Statement] = []
    for comp in dict.fromkeys(c for c in (s.root_component, s.affected) if c):
        exc = s.app_exceptions(comp)
        if exc:
            e = exc[0]
            site = e.data.get("crash_site") or {}
            questions.append(Statement(f"Examine the application defect in {comp}: unhandled {e.data['exc_type']}"
                                       + (f" in {site.get('func')}() at {site.get('file')}:{site.get('line')}" if site else ""),
                                       [x.id for x in exc[:2]]))
            again = s.recurs_on_start(comp)
            if again:
                questions.append(Statement(f"A restart of {comp} is not proposed: the failure recurs on every start",
                                           [f.id for f in again]))
        changes = s.recorded_changes(comp)
        if changes and comp != s.affected:
            questions.append(Statement(f"Find out why {comp} changed ({changes[0].text[:100]})", [changes[0].id]))
        pressure = s.resource_pressure(comp)
        if pressure and any(a.type == ActionType.ADJUST_RESOURCE_LIMIT for a in corrective):
            questions.append(Statement(f"Find out why {comp}'s memory grows under load before relying on a higher limit",
                                       [f.id for f in pressure[:2]]))
    if s.outage() and not s.endpoint_missing_now() and s.root_component and s.desired_now(s.root_component)[0]:
        questions.append(Statement(f"{s.root_component} was unavailable during the incident and is available now: "
                                   f"no change to it is needed now; consider how {s.affected} should behave while "
                                   f"it is unavailable", [s.outage().id]))
    if s.root_kind == "configuration" and s.dependency:
        changed = [c for c in s.find("config_changed") if c.data.get("config_source") == s.dependency["source"]]
        if changed:
            questions.append(Statement(f"Find out why {s.dependency['source']} was changed shortly before the failures",
                                       [changed[0].id]))
    for a in s.competing():
        questions.append(Statement(f"Rule out the competing explanation: {a['label']} in {a['component']} "
                                   f"(score {a['score']:.2f})"))
    if not questions and not corrective and not gated:
        # Nothing specific is known to fix or to rule out: point at what the diagnosis rests on.
        questions.append(Statement(f"No supported typed action addresses '{s.dx.get('category_label')}' in "
                                   f"{s.affected}; examine the evidence for it: {s.dx.get('root_cause', '')[:120]}",
                                   list(s.dx.get("evidence") or [])[:4]))
    if gated:
        questions.insert(0, Statement(f"The diagnosis is not established strongly enough "
                                      f"({s.dx.get('confidence_label')}, {s.dx.get('confidence', 0):.0%}) to justify a "
                                      f"change: gather more evidence first"))
    if not questions:
        return None
    return ProposedAction(
        type=ActionType.INVESTIGATE_FURTHER, target={"component": s.root_component or s.affected},
        parameters={"questions": [q.statement for q in questions]}, parameters_complete=True,
        urgency=Urgency.FOLLOW_UP, summary="Investigate further (no change to the system)", rationale=questions,
        preconditions=[], expected_final_state=["The open questions are answered by evidence"], risks=[],
        rollback=Rollback("none_needed", "Nothing is changed"), verification=[])


def _verify_component(comp: str | None) -> list[VerificationCriterion]:
    if not comp:
        return []
    return [VerificationCriterion(Check.COMPONENT_READY, comp, {"ready_equals_desired": True},
                                  f"All {comp} instances are ready")]


def _verify_entry(s: Situation) -> list[VerificationCriterion]:
    out = []
    if s.find("entry_probe"):
        out.append(VerificationCriterion(Check.ENTRY_REQUESTS_SUCCEED, s.find("entry_probe")[0].subject.split("/", 1)[1],
                                         {"status_below": 500}, "Synthetic user requests succeed"))
    entry = s.entry_component()
    if entry:
        out.append(VerificationCriterion(Check.ERROR_RATIO_BELOW, entry, {"ratio": 0.05},
                                         f"The user-facing error ratio at {entry} stays below 5%"))
    return out


CANDIDATES = (_resource_limit, _restore_capacity, _restore_configuration, _scale_for_load)


# --------------------------------------------------------------------------- the plan

def plan_remediation(dx: dict, reconstruction: dict | None, impact: dict | None, store: EvidenceStore,
                     incident_id: str = "") -> RemediationPlan:
    s = Situation(dx, reconstruction, impact, store)
    gated = dx.get("category") in (None, "undetermined") or dx.get("confidence_label") == "Low"
    corrective = [] if gated else [a for a in (rule(s) for rule in CANDIDATES) if a is not None]
    investigate = _investigate(s, corrective, gated)
    state = _incident_state(s, corrective)
    immediate = [a for a in corrective if a.urgency == Urgency.IMMEDIATE]
    if gated:
        assessment = Assessment.INVESTIGATE_FURTHER
        reason = (f"The evidence does not establish a cause strongly enough to justify a change "
                  f"({dx.get('category_label') or 'undetermined'}, confidence {dx.get('confidence', 0):.0%})")
    elif immediate:
        assessment = Assessment.ACTION_PROPOSED
        reason = f"{len(immediate)} action(s) address a condition that still held when the evidence was collected"
    elif state == IncidentState.ACTIVE:
        assessment = Assessment.NO_SAFE_ACTION
        reason = (f"The failure is still present, but no supported typed action addresses "
                  f"'{dx.get('category_label')}'; the plan lists what to investigate")
    else:
        assessment = Assessment.NO_IMMEDIATE_ACTION
        reason = ("No condition that requires a change holds now" + (
            f"; {len(corrective)} preventive action(s) would reduce the chance of recurrence" if corrective else ""))
    actions = corrective + ([investigate] if investigate else [])
    rationale = [Statement(r["statement"], list(r.get("facts", []))) for r in (dx.get("reasoning") or [])[:4]]
    cited = sorted({fid for a in actions for st in a.rationale + a.risks for fid in st.fact_ids}
                   | {fid for st in rationale for fid in st.fact_ids}
                   | {fid for st in _current_state(s) for fid in st.fact_ids}, key=_fid_order)
    return RemediationPlan(
        incident_id=incident_id,
        diagnosis={"category": dx.get("category"), "category_label": dx.get("category_label"),
                   "root_cause": dx.get("root_cause"), "root_cause_component": dx.get("root_cause_component"),
                   "affected_component": s.affected, "impacted_components": list(dx.get("impacted_components") or []),
                   "confidence": dx.get("confidence"), "confidence_label": dx.get("confidence_label")},
        incident_state=state, current_state=_current_state(s), assessment=assessment, assessment_reason=reason,
        actions=actions, rationale=rationale,
        uncertainty=Uncertainty(
            confidence=dx.get("confidence", 0.0), confidence_label=dx.get("confidence_label", "Low"),
            competing_causes=[{"category": a["category"], "component": a["component"], "score": a["score"]}
                              for a in s.competing()],
            evidence_gaps=[g.text for g in s.gaps()],
            notes=[f"{a.type.value}: some parameters cannot be determined from the evidence and must be supplied by "
                   f"an engineer" for a in corrective if not a.parameters_complete]
            + ["The plan describes the state when the evidence was collected; re-check before acting"]),
        evidence=[{"id": fid, "text": store.by_id(fid).text} for fid in cited if store.by_id(fid)])


def plan_rollback(plan: dict, action_index: int, applied: dict, outcome: str, reason: str,
                  rollback_id: str) -> RemediationPlan | None:
    """A one-action plan that puts back the value recorded immediately before an executed change (Iteration 7).

    Offered only after verification did not confirm the change (NOT_RESOLVED / INCONCLUSIVE). It is a proposal like
    any other: it must be approved and executed through the same controls. `applied` is the execution's recorded
    result ({"before", "after"} as the provider reported them). Returns None if the previous value is unknown."""
    a = plan["actions"][action_index]
    before, after = applied.get("before"), applied.get("after")
    if before in (None, "") or after in (None, ""):
        return None
    t = ActionType(a["type"])
    comp = (a["target"] or {}).get("component") or (plan.get("diagnosis") or {}).get("affected_component")
    why = [Statement(f"The executed change ({after} replaced {before}) was verified {outcome}: {reason}")]
    if t == ActionType.ADJUST_RESOURCE_LIMIT:
        cur, prev = _bytes(after), _bytes(before)
        if cur is None or prev is None:
            return None
        action = ProposedAction(
            type=t, target=dict(a["target"]), parameters={"resource": "memory", "current_limit_bytes": cur,
                                                          "current_limit": _mib(cur), "proposed_limit_bytes": prev,
                                                          "proposed_limit": _mib(prev)},
            parameters_complete=True, urgency=Urgency.IMMEDIATE,
            summary=f"Roll back {comp}'s memory limit from {_mib(cur)} to the previous {_mib(prev)}",
            rationale=why, preconditions=[f"The live memory limit is still {_mib(cur)}"],
            expected_final_state=[f"{comp}'s memory limit is {_mib(prev)} again"],
            risks=[Statement(f"The original failure (kills at {_mib(prev)}) is expected to recur under the same load")],
            rollback=Rollback("restore_previous_value", f"Re-apply {_mib(cur)}", {"limit_bytes": cur}),
            verification=_verify_component(comp))
    elif t == ActionType.SCALE_WORKLOAD:
        cur, prev = int(after), int(before)
        action = ProposedAction(
            type=t, target=dict(a["target"]), parameters={"current_replicas": cur, "target_replicas": prev},
            parameters_complete=True, urgency=Urgency.IMMEDIATE,
            summary=f"Roll back {comp}'s replicas from {cur} to the previous {prev}",
            rationale=why, preconditions=[f"{comp} still has {cur} desired replica(s)"],
            expected_final_state=[f"{comp} has {prev} desired replica(s) again"],
            risks=[Statement(f"With {prev} replica(s), the original failure is expected to recur")],
            rollback=Rollback("restore_previous_value", f"Scale {comp} to {cur} again", {"replicas": cur}),
            verification=_verify_component(comp) if prev > 0 else [])
    elif t == ActionType.RESTORE_CONFIGURATION:
        item, source = a["target"].get("config_item"), a["target"].get("source")
        action = ProposedAction(
            type=t, target=dict(a["target"]),
            parameters={"current_host": after, "restore_to": None, "restore_to_host": before},
            parameters_complete=True, urgency=Urgency.IMMEDIATE,
            summary=f"Roll back the host in {item} ({source}) from '{after}' to the previous '{before}'",
            rationale=why, preconditions=[f"{item} still points to '{after}'"],
            expected_final_state=[f"{item} points to '{before}' again"],
            risks=[Statement(f"'{before}' is the value diagnosed as faulty: the dependency is expected to be "
                             f"unreachable again")],
            rollback=Rollback("restore_previous_value", f"Point {item} to '{after}' again", {"host": after}),
            verification=_verify_component((plan.get("diagnosis") or {}).get("affected_component")))
    else:
        return None
    dx = plan.get("diagnosis") or {}
    return RemediationPlan(
        incident_id=rollback_id, diagnosis=dict(dx), incident_state=IncidentState.UNKNOWN,
        current_state=[Statement(f"After the executed change: {reason}")],
        assessment=Assessment.ACTION_PROPOSED,
        assessment_reason=f"Rollback offered because verification of the executed change was {outcome}",
        actions=[action], rationale=why,
        uncertainty=Uncertainty(confidence=dx.get("confidence") or 0.0, confidence_label=dx.get("confidence_label")
                                or "Low", notes=["A rollback restores the previous value; it does not fix the "
                                                 "original failure"]),
        evidence=[], generated_by="deterministic rollback planner (restores the value recorded before the executed "
                                  "change; plans only, never executes)")


_QTY = re.compile(r"\s*(\d+(?:\.\d+)?)\s*(Ki|Mi|Gi)?\s*")


def _bytes(q) -> float | None:
    m = _QTY.fullmatch(str(q))
    return float(m.group(1)) * {None: 1, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30}[m.group(2)] if m else None


def _incident_state(s: Situation, corrective: list[ProposedAction]) -> IncidentState:
    """Active if a failure condition holds now; recovered if the reconstruction saw recovery and none holds."""
    w = s.rc.get("window") or {}
    now = (w.get("ongoing") or s.failing_now(s.affected) or s.failing_now(s.root_component)
           or (s.root_component and s.root_component != s.affected and s.desired_now(s.root_component)[0] == 0)
           or (s.root_kind == "configuration" and s.endpoint_missing_now() is not None))
    if now:
        return IncidentState.ACTIVE
    if s.dx.get("category") in (None, "undetermined"):
        return IncidentState.UNKNOWN          # no established failure: "recovered" would assert there was one
    if w.get("incident_end") or (s.ready_now(s.affected) and (not s.root_component or s.ready_now(s.root_component))):
        return IncidentState.RECOVERED
    return IncidentState.UNKNOWN


def _current_state(s: Situation) -> list[Statement]:
    out = []
    for comp in dict.fromkeys(c for c in (s.affected, s.root_component) if c):
        st = s.status(comp)
        if st:
            out.append(Statement(f"{comp}: {st.text}" + (" - failing now" if s.failing_now(comp) else ""), [st.id]))
    if s.dependency:
        subj = f"dependency/{s.dependency['endpoint']}"
        eps = next(iter(s.find("service_endpoints", subj)), None)
        probe = next((p for p in s.find("connectivity_probe", subj) if not p.data.get("alternative_for")), None)
        missing = s.endpoint_missing_now()
        for f in (missing, eps, probe):
            if f:
                out.append(Statement(f"{s.dependency['endpoint']}: {f.text}", [f.id]))
        if s.outage():
            out.append(Statement(f"During the incident (recorded history): {s.outage().text}", [s.outage().id]))
    return out


def _fid_order(fid: str):
    return int(fid[1:]) if fid[1:].isdigit() else 0
