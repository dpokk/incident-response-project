"""Execution of an approved remediation action (Iteration 7). The ONLY component that holds a cluster writer.

    Execute request (a person, explicitly)
      1 authorization          the requester is an allowed executor
      2 approval validity      an effective *approval* of this action on this exact, current plan digest
      3 typed change           built from the plan action + the approved, engineer-supplied value
      4 plan age               within the policy maximum
      5 policy                 action type, namespace, caps
      6 claim                  one execution per (incident, digest, action), atomically - never twice
      7 live recheck           fresh reads through the capability layer (recheck.py)
      8 dry run                provider-validated, recorded
      9 apply ONE change       compare-and-set; any doubt after sending it -> uncertain
     10 verification           the plan's own criteria over a bounded window (Milestone 3)

Steps 1-5 refuse without claiming (the attempt is recorded). From step 6 on, the claim is permanent: a refusal,
a failure or an interruption ends that action of that plan, and acting again needs a fresh investigation and plan.
Nothing here decides *what* to change - that is the plan, approved by a human; nothing here retries.
"""
import time

from .actuators.base import Actuator, ChangeResult
from .execution_model import ChangeRequest, ExecutionRequest, ExecutionResult, Refusal, Status, memory_bytes
from .execution_policy import ExecutionPolicy
from .execution_store import ExecutionStore, execution_id
from .recheck import host_of, recheck

EXECUTABLE = ("adjust_resource_limit", "scale_workload", "restore_configuration")


class ExecutionService:
    def __init__(self, review, store: ExecutionStore, policy: ExecutionPolicy, actuator: Actuator | None,
                 capabilities=None, clock=time.time, log=print):
        """`capabilities`: a callable returning a fresh read-only Capabilities object (one per recheck/verification).
        `actuator`: the cluster writer; None means execution is unavailable (checks still run and refuse)."""
        self.review, self.store, self.policy, self.actuator = review, store, policy, actuator
        self.capabilities, self.clock, self.log = capabilities, clock, log

    # ----------------------------------------------------------------------------- the typed change
    @staticmethod
    def change_for(plan: dict, index: int, decision: dict) -> tuple[ChangeRequest | None, Refusal | None, str]:
        """The one typed change an approved action stands for. Values come from the plan, or - where the plan could
        not supply them - from the value the approver typed. Candidate values are never used."""
        action = plan["actions"][index]
        atype, p, target = action["type"], action["parameters"], action["target"]
        supplied = decision.get("supplied_parameters") or {}
        if atype not in EXECUTABLE:
            return None, Refusal.UNSUPPORTED_ACTION, f"{atype} is not an executable action type"
        if atype == "adjust_resource_limit":
            new = memory_bytes(supplied.get("proposed_limit")) if not action["parameters_complete"] \
                else p.get("proposed_limit_bytes")
            if new is None or p.get("current_limit_bytes") is None or not target.get("process"):
                return None, Refusal.MISSING_PARAMETER, "the approved memory limit or the current limit is unknown"
            return ChangeRequest(atype, target["component"], process=target["process"],
                                 expected_bytes=float(p["current_limit_bytes"]), target_bytes=float(new)), None, ""
        if atype == "scale_workload":
            new = supplied.get("target_replicas") if not action["parameters_complete"] else p.get("target_replicas")
            if new is None or p.get("current_replicas") is None:
                return None, Refusal.MISSING_PARAMETER, "the approved replica count or the current count is unknown"
            return ChangeRequest(atype, target["component"], expected_replicas=int(p["current_replicas"]),
                                 target_replicas=int(new)), None, ""
        consumer = (plan.get("diagnosis") or {}).get("affected_component")
        host = supplied.get("restore_to_host") if not action["parameters_complete"] else None
        value = p.get("restore_to") if action["parameters_complete"] else None
        if not (host or value) or not p.get("current_host") or not consumer:
            return None, Refusal.MISSING_PARAMETER, "the value to restore, the current host or the consumer is unknown"
        return ChangeRequest(atype, consumer, source=target.get("source"), item=target.get("config_item"),
                             expected_host=p["current_host"], target_host=host, target_value=value), None, ""

    # ----------------------------------------------------------------------------- steps 1-5 (no claim)
    def preflight(self, req: ExecutionRequest) -> tuple[ExecutionResult | None, dict]:
        """Run the checks that need no claim. Returns (refusal or None, context for the next steps)."""
        checks: list[dict] = []

        def refuse(code: Refusal, msg: str) -> tuple[ExecutionResult, dict]:
            checks.append({"check": code.value, "ok": False, "detail": msg})
            return ExecutionResult(False, code.value, msg, checks=checks), {}

        if not self.policy.may_execute(req.executor, self.review.approvers):
            return refuse(Refusal.UNAUTHORIZED, "You are not an authorised executor; nothing was executed.")
        checks.append({"check": "authorization", "ok": True, "detail": "requester is an authorised executor"})
        rec = self.review.plan(req.incident_id, req.plan_digest)
        if rec is None or not 0 <= req.action_index < len(rec["plan"]["actions"]):
            return refuse(Refusal.UNKNOWN_PLAN, "This plan or action is not known.")
        if rec["superseded_by"] or self.review.current_digest(req.incident_id) != req.plan_digest:
            return refuse(Refusal.SUPERSEDED, "A newer plan exists for this incident: approvals of this plan no longer "
                                              "apply. Review the current plan.")
        decision = self.review.effective_decision(req.incident_id, req.plan_digest, req.action_index)
        if not decision or decision["decision"] != "approved":
            return refuse(Refusal.NOT_APPROVED, "This action has no effective approval on this plan"
                          + (f" (it was {decision['decision']})" if decision else "") + "; nothing was executed.")
        checks.append({"check": "approval", "ok": True, "detail": f"approved by <@{decision['reviewer']}> on plan "
                                                                   f"{req.plan_digest[:12]} (current)"})
        change, code, msg = self.change_for(rec["plan"], req.action_index, decision)
        if change is None:
            return refuse(code, msg)
        ok, detail = self.policy.plan_age_ok(rec["collected_at"], self.clock())
        if not ok:
            return refuse(Refusal.STALE_PLAN, detail[0].upper() + detail[1:] + ".")
        checks.append({"check": "plan_age", "ok": True, "detail": detail})
        scope = self.actuator.scope if self.actuator else ""
        pol = self.policy.check(change, scope)
        checks += [{**c, "check": f"policy:{c['check']}"} for c in pol.checks]
        if not pol.allowed:
            return ExecutionResult(False, Refusal.POLICY.value, "Action exceeds execution policy: "
                                   + "; ".join(pol.reasons) + ".", checks=checks), {}
        return None, {"plan": rec, "decision": decision, "change": change, "policy": pol, "checks": checks}

    # ----------------------------------------------------------------------------- the request
    def execute(self, req: ExecutionRequest) -> ExecutionResult:
        existing = self.store.for_action(req.incident_id, req.plan_digest, req.action_index)
        if existing is not None:      # checked first, so a repeat never re-runs anything (also enforced by the claim)
            return self._refuse_attempt(req, self._existing(existing))
        refusal, ctx = self.preflight(req)
        if refusal is not None:
            return self._refuse_attempt(req, refusal)
        change: ChangeRequest = ctx["change"]
        claimed, record = self.store.claim(req, change.action_type, ctx["decision"], change.to_dict(),
                                           {"allowed": True, "checks": ctx["policy"].checks})
        if not claimed:
            return self._refuse_attempt(req, self._existing(record))
        eid = record["execution_id"]
        self.store.attempt(req, "claimed", change.describe(), eid)
        return self._run(eid, req, ctx)

    def _run(self, eid: str, req: ExecutionRequest, ctx: dict) -> ExecutionResult:
        """Steps 7-9 on a claimed execution. Before the real write nothing has changed, so a refusal is final but
        certain; once the real write has been sent, any doubt is recorded as `uncertain` - never retried."""
        change: ChangeRequest = ctx["change"]
        checks = list(ctx["checks"])

        def stop(status: Status, code: str, message: str, **fields) -> ExecutionResult:
            rec = self.store.update(eid, status, refusal=code if status == Status.REFUSED else None,
                                    message=message, **fields)
            self.log(f"execution {eid}: {code}: {message}")
            return ExecutionResult(False, code, message, eid, checks, rec)

        # 7 live recheck (fresh reads through the capability layer)
        if self.actuator is None or self.capabilities is None:
            return stop(Status.REFUSED, Refusal.RECHECK_FAILED.value, "Execution is not available in this process "
                        "(no cluster connection); nothing was changed.")
        rc = recheck(self.capabilities(), change, self.clock(), self.policy.max_plan_age_s,
                     require_relevance=ctx.get("require_relevance", True))
        checks += [{**c, "check": f"recheck:{c['check']}"} for c in rc.checks]
        if not rc.ok:
            return stop(Status.REFUSED, rc.code.value, rc.message, recheck=rc.to_dict())
        self.store.update(eid, recheck=rc.to_dict())

        # 8 dry run (validated and admitted by the provider, nothing persisted)
        try:
            dry = self._call(change, rc.write, dry_run=True)
        except Exception as exc:  # noqa: BLE001 - nothing was sent for real
            dry = ChangeResult(False, True, change.action_type, change.component, error=f"{type(exc).__name__}: {exc}")
        dry_d = self._redact(change, dry)
        checks.append({"check": "dry_run", "ok": dry.accepted, "detail": "accepted by the provider" if dry.accepted
                       else f"rejected: {dry.error}"})
        if not dry.accepted or dry.changed:
            return stop(Status.REFUSED, Refusal.DRY_RUN_FAILED.value, f"Dry run failed ({dry.error}); nothing was "
                        f"changed.", dry_run=dry_d)
        self.store.update(eid, Status.APPLYING, dry_run=dry_d)

        # 9 apply exactly ONE typed change (compare-and-set)
        try:
            res = self._call(change, rc.write, dry_run=False)
        except Exception as exc:  # noqa: BLE001 - the request may or may not have taken effect
            return stop(Status.UNCERTAIN, Status.UNCERTAIN.value, f"The change request failed unexpectedly "
                        f"({type(exc).__name__}); whether it took effect is unknown. Not retried: run a fresh "
                        f"investigation.", applied={"error": f"{type(exc).__name__}: {exc}"})
        applied = self._redact(change, res)
        if not res.accepted:
            if res.changed:
                return stop(Status.UNCERTAIN, Status.UNCERTAIN.value, f"The change was only partly applied "
                            f"({res.error}). Not retried: run a fresh investigation.", applied=applied)
            return stop(Status.APPLY_FAILED, Status.APPLY_FAILED.value, f"The provider rejected the change "
                        f"({res.error}); nothing was changed.", applied=applied)
        rec = self.store.update(eid, Status.VERIFYING, applied={**applied, "at": self.clock()},
                                message=f"applied: {change.describe()}")
        self.log(f"execution {eid}: applied {change.describe()}; verifying")
        return ExecutionResult(True, Status.VERIFYING.value, f"Applied: {change.describe()}. Verifying.", eid, checks,
                               rec)

    def _call(self, change: ChangeRequest, w: dict, dry_run: bool) -> ChangeResult:
        """The single dispatch from a typed change to one typed actuator operation."""
        if change.action_type == "adjust_resource_limit":
            return self.actuator.set_memory_limit(w["component"], w["process"], w["expected_bytes"], w["new_bytes"],
                                                  dry_run=dry_run)
        if change.action_type == "scale_workload":
            return self.actuator.set_replicas(w["component"], w["expected"], w["new"], dry_run=dry_run)
        if change.action_type == "restore_configuration":
            return self.actuator.set_config_value(w["source"], w["item"], w["expected_value"], w["new_value"],
                                                  w["restart_component"], dry_run=dry_run)
        raise ValueError(f"unsupported action type {change.action_type}")

    @staticmethod
    def _redact(change: ChangeRequest, res: ChangeResult) -> dict:
        """Configuration values are recorded as their host only (a value may contain more than the host)."""
        d = res.to_dict()
        if change.action_type == "restore_configuration":
            d["before"], d["after"] = host_of(res.before), host_of(res.after)
        return d

    # ----------------------------------------------------------------------------- helpers
    @staticmethod
    def _existing(record: dict) -> ExecutionResult:
        status = record["status"]
        if status == "uncertain":
            code, msg = Refusal.UNCERTAIN, ("An earlier execution of this action was interrupted; its effect is "
                                            "uncertain. It is never retried: run a fresh investigation.")
        elif status in ("claimed", "applying", "verifying"):
            code, msg = Refusal.IN_PROGRESS, "This action is already being executed; nothing was started again."
        else:
            code, msg = Refusal.ALREADY_COMPLETED, (f"This action of this plan was already executed or refused "
                                                    f"({status}{', ' + record['outcome'] if record.get('outcome') else ''})"
                                                    f"; it is not executed again. A fresh plan is needed to act again.")
        return ExecutionResult(False, code.value, msg, execution_id=record["execution_id"], record=record)

    def _refuse_attempt(self, req: ExecutionRequest, result: ExecutionResult) -> ExecutionResult:
        self.store.attempt(req, result.code, result.message, result.execution_id)
        self.log(f"execution: {result.code} on {req.incident_id} action {req.action_index + 1} by {req.executor}: "
                 f"{result.message}")
        return result


def request_id(req: ExecutionRequest) -> str:
    return execution_id(req.incident_id, req.plan_digest, req.action_index)
