"""The Remediation Agent: graduated autonomy for known, high-confidence incidents (demo only).

It is deterministic - no LLM decides anything here. It replaces the engineer's two Slack clicks (Approve, Execute)
only when a policy gate passes, and then drives the SAME Iteration 7 executor an engineer would:

    gate (every check must pass)                       demo/remediation_agent_policy.json
      automatic remediation switched on · known pattern (rules lead) · rule confidence ≥ threshold ·
      the Investigator Agent agrees · every finding evidence-checked · action type allowed for automation ·
      a validated value · the component is not locked by an unacknowledged failed attempt
    attempts (a ladder written in policy, never more than max_attempts)
      each attempt = recorded approval by this agent → executor: policy → claim → live recheck → dry run →
      ONE compare-and-set change → verification (RESOLVED / NOT_RESOLVED / INCONCLUSIVE)
      RESOLVED                      → done
      NOT_RESOLVED / INCONCLUSIVE   → next step of the ladder (a different value; the same value is never retried)
      refused / failed / uncertain  → stop (an uncertain state is never retried and never reverted)
    revert (always, when anything was changed and nothing failed) → the value before the first attempt
    handover → Slack: what was tried, the revert, an Acknowledge button; the component stays locked against
               automatic remediation until an engineer acknowledges

An engineer can press Stop at any time: no further attempt and no revert is made, and the engineer owns the incident.

The agent never touches the cluster itself: every change goes through `ExecutionService.execute`, which performs all
of its safety checks exactly as for a human request. Its identity (`AGENT_ID`) is a configured executor, recorded in
the same audit as a person's clicks.
"""
import copy
import json
import math
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from investigator.execution_model import ExecutionRequest, memory_bytes, mib
from investigator.remediation import plan_rollback

AGENT_ID = "remediation-agent"           # recorded as reviewer/executor; never a Slack user ID (those start with U/W)
AGENT_NAME = "Remediation Agent"
STEP_ROUND = 64 * 2**20                  # memory ladder steps are rounded up to 64Mi
REVERT_KEY = "#rollback-auto"            # contains executor.ROLLBACK_MARK: the executor treats it as a rollback


# ----------------------------------------------------------------------------- policy

@dataclass(frozen=True)
class AutoPolicy:
    enabled: bool = True
    min_rule_confidence: float = 0.9
    actions: dict = field(default_factory=lambda: {
        "adjust_resource_limit": {"max_attempts": 3, "step_factor": 1.5},
        "scale_workload": {"max_attempts": 1}})

    @classmethod
    def load(cls, path: Path) -> "AutoPolicy":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(bool(d.get("enabled", True)), float(d.get("min_rule_confidence", 0.9)),
                   {k: v for k, v in (d.get("actions") or {}).items() if not k.startswith("_")})


def first_value(proposal: dict):
    """The value of the first attempt: the validated value the Investigator Agent proposed, or the plan's own value
    when the plan is complete. None when there is no value (then automation is not allowed)."""
    par = proposal.get("parameter")
    if par is not None:
        return par.get("value")
    p = proposal.get("parameters") or {}
    return {"adjust_resource_limit": p.get("proposed_limit"),
            "scale_workload": p.get("target_replicas"),
            "rollback_release": p.get("previous_image")}.get(proposal.get("action_type"))


def ladder(policy: AutoPolicy, proposal: dict, max_memory_bytes: float) -> list:
    """The values to try, in order. Memory: the first value, then x step_factor (rounded up to 64Mi), never above the
    execution policy's cap. Replicas: a single step - more replicas of a workload is not a fix for a cause the first
    count did not fix (and is unsafe for a stateful one such as a database)."""
    atype, v0 = proposal.get("action_type"), first_value(proposal)
    rule = policy.actions.get(atype) or {}
    n = int(rule.get("max_attempts", 1))
    if v0 is None or n < 1:
        return []
    if atype == "adjust_resource_limit":
        b = memory_bytes(v0)
        if b is None:
            return []
        out = [b]
        factor = float(rule.get("step_factor", 1.5))
        while len(out) < n:
            nxt = math.ceil(out[-1] * factor / STEP_ROUND) * STEP_ROUND
            if nxt > max_memory_bytes or nxt <= out[-1]:
                break
            out.append(nxt)
        return [mib(b) for b in out]
    if atype == "scale_workload":
        return [int(v0)]
    return []


def gate(policy: AutoPolicy, enabled: bool, route: dict, ai: dict, proposal: dict, locked: dict) -> dict:
    """Whether the Remediation Agent may act without a human click. Every check is evaluated and shown."""
    checks = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    add("enabled", enabled, "automatic remediation is switched on" if enabled
        else "automatic remediation is switched off: human approval only")
    route = route or {}
    add("known_pattern", route.get("mode") == "verify",
        "known pattern: the rule engine led" if route.get("mode") == "verify"
        else "not a known pattern: the Investigator Agent led")
    conf = float(route.get("rule_confidence") or 0)
    add("rule_confidence", conf >= policy.min_rule_confidence,
        f"rule engine confidence {conf:.0%} (needs ≥ {policy.min_rule_confidence:.0%})")
    rep = (ai or {}).get("report") if (ai or {}).get("status") == "ok" else None
    add("agent_report", rep is not None, "the Investigator Agent produced a validated report" if rep
        else "the Investigator Agent produced no report")
    agree = (rep or {}).get("agreement") or {}
    add("agent_agrees", bool(agree.get("category") and agree.get("component")),
        "the Investigator Agent independently agrees with the rule engine" if agree.get("category")
        and agree.get("component") else "the Investigator Agent does not agree with the rule engine")
    v = (rep or {}).get("validation") or {}
    n, s = int(v.get("findings") or 0), int(v.get("supported_findings") or 0)
    add("evidence_checked", bool(rep) and v.get("ok") and n > 0 and s == n,
        f"{s}/{n} findings supported by collected evidence" + ("" if v.get("ok", True) else
                                                              " · " + "; ".join(v.get("problems") or [])))
    atype = proposal.get("action_type") if proposal.get("executable") else None
    add("action_allowed", atype in policy.actions,
        f"{atype} is allowed for automatic remediation" if atype in policy.actions else
        (f"{atype} needs a human (automation allows: {', '.join(sorted(policy.actions))})" if atype
         else "no executable typed action"))
    value = first_value(proposal) if atype else None
    par = proposal.get("parameter") or {}
    add("value", value is not None, f"value to apply: {value}" if value is not None
        else (par.get("error") or "no validated value to apply"))
    comp = (proposal.get("target") or {}).get("component")
    lock = locked.get(comp) if comp else None
    add("not_locked", lock is None,
        f"{comp or 'the target'} has no unacknowledged failed automatic remediation" if lock is None
        else f"{comp} is locked: automatic remediation failed on {lock['incident_id']} and is not acknowledged")
    return {"eligible": all(c["ok"] for c in checks), "checks": checks}


# ----------------------------------------------------------------------------- audit + locks

class AutoStore:
    """The agent's own audit (every event) and component locks (the circuit breaker)."""

    def __init__(self, path: Path, clock=time.time):
        self.clock = clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS auto_events (id INTEGER PRIMARY KEY, incident_id TEXT, at REAL, kind TEXT,
                actor TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS auto_locks (component TEXT, incident_id TEXT, locked_at REAL, reason TEXT,
                acknowledged_by TEXT, acknowledged_at REAL);""")
        self._db.commit()

    def _w(self, sql, args=()):
        with self._lock:
            self._db.execute(sql, args)
            self._db.commit()

    def _q(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def event(self, incident_id: str, kind: str, actor: str, detail: dict) -> None:
        self._w("INSERT INTO auto_events (incident_id, at, kind, actor, detail) VALUES (?,?,?,?,?)",
                (incident_id, self.clock(), kind, actor, json.dumps(detail, default=str)))

    def events(self, incident_id: str) -> list[dict]:
        return self._q("SELECT * FROM auto_events WHERE incident_id=? ORDER BY id", (incident_id,))

    def lock(self, component: str, incident_id: str, reason: str) -> None:
        self._w("INSERT INTO auto_locks VALUES (?,?,?,?,NULL,NULL)", (component, incident_id, self.clock(), reason))

    def locked(self) -> dict:
        rows = self._q("SELECT * FROM auto_locks WHERE acknowledged_at IS NULL ORDER BY locked_at")
        return {r["component"]: r for r in rows}

    def acknowledge(self, incident_id: str, user: str) -> list[str]:
        """Acknowledge every open lock of this incident; returns the components released."""
        comps = [r["component"] for r in self._q("SELECT component FROM auto_locks WHERE incident_id=? AND "
                                                  "acknowledged_at IS NULL", (incident_id,))]
        self._w("UPDATE auto_locks SET acknowledged_by=?, acknowledged_at=? WHERE incident_id=? AND "
                "acknowledged_at IS NULL", (user, self.clock(), incident_id))
        return comps


# ----------------------------------------------------------------------------- the agent

def retry_plan(plan: dict, index: int, current, value, key: str, n: int) -> dict:
    """A one-action plan for attempt n (n ≥ 2): the same action and verification, with the live value the previous
    attempt left (`current`, the compare-and-set precondition) and the next value of the ladder."""
    p = copy.deepcopy(plan)
    a = p["actions"][index]
    comp = (a.get("target") or {}).get("component")
    if a["type"] == "adjust_resource_limit":
        cur, new = memory_bytes(current), memory_bytes(value)
        a["parameters"].update(current_limit_bytes=cur, current_limit=mib(cur), proposed_limit_bytes=new,
                               proposed_limit=mib(new))
        a["summary"] = f"Attempt {n}: raise {comp}'s memory limit from {mib(cur)} to {mib(new)}"
    else:
        a["parameters"].update(current_replicas=int(current), target_replicas=int(value))
        a["summary"] = f"Attempt {n}: scale {comp} from {current} to {value} replicas"
    a["parameters_complete"] = True
    p["actions"] = [a]
    p["incident_id"] = f"{key}#auto-{n}"
    return p


class RemediationAgent:
    def __init__(self, review, execution, store: AutoStore, emit, say=None, clock=time.time, log=print):
        """`emit(type, data)`: console events. `say(kind, data)`: Slack messages (the engine renders them)."""
        self.review, self.execution, self.store = review, execution, store
        self.emit, self.say, self.clock, self.log = emit, say or (lambda k, d: None), clock, log
        self._stop: dict[str, str] = {}               # incident -> who pressed Stop
        self.running: str | None = None

    def stop(self, incident_id: str, user: str) -> bool:
        if self.running != incident_id or incident_id in self._stop:
            return False
        self._stop[incident_id] = user
        self.store.event(incident_id, "stop", user, {})
        self.emit("auto_stop", {"by": user, "at": self.clock()})
        self.say("stopped", {"by": user})
        return True

    def run(self, inc: dict, proposal: dict, values: list, gate_result: dict) -> dict:
        key, digest, idx = inc["id"], inc["digest"], proposal["plan_index"]
        self.running = key
        try:
            return self._run(key, digest, idx, proposal, values, gate_result)
        finally:
            self.running = None

    def _run(self, key, digest, idx, proposal, values, gate_result) -> dict:
        plan = self.review.plan(key, digest)["plan"]
        action = plan["actions"][idx]
        comp = (action.get("target") or {}).get("component")
        self.store.event(key, "start", AGENT_ID, {"gate": gate_result, "ladder": values})
        self.emit("auto_start", {"ladder": [str(v) for v in values], "action_type": action["type"],
                                 "component": comp, "at": self.clock()})
        self.say("start", {"ladder": values, "proposal": proposal, "gate": gate_result})
        attempts, original, current, changed, stop_reason = [], None, None, False, None
        uncertain = False
        resolved = None
        for n, value in enumerate(values, 1):
            if key in self._stop:
                stop_reason = f"stopped by {self._stop[key]}"
                break
            if n == 1:
                k, d, i = key, digest, idx
                supplied = None if action["parameters_complete"] else str(value)
            else:
                k = f"{key}#auto-{n}"
                d, i, supplied = self.review.register_plan(k, retry_plan(plan, idx, current, value, key, n),
                                                           self.clock()), 0, None
            dec = self.review.decide(k, d, i, "approved", AGENT_ID, supplied=supplied,
                                     comment="approved automatically: the Remediation Agent's policy gate passed")
            att = {"n": n, "of": len(values), "value": str(value), "key": k}
            if not dec.effective:
                att.update(code="NOT_APPROVED", message=dec.message)
                attempts.append(att)
                stop_reason = f"the automatic approval was not recorded: {dec.message}"
                self.emit("auto_attempt", {**att, "stage": "stopped"})
                break
            self.store.event(key, "approved", AGENT_ID, {"attempt": n, "value": str(value), "plan_key": k,
                                                          "digest": d})
            self.emit("auto_attempt", {**att, "stage": "approved", "at": self.clock()})
            self.say("attempt", {**att, "stage": "approved"})
            res = self.execution.execute(ExecutionRequest(k, d, i, AGENT_ID, self.clock()),
                                         progress=lambda stage, data, a=att: self._progress(a, stage, data))
            rec = (self.execution.store.get(res.execution_id) if res.execution_id else None) or {}
            applied = rec.get("applied") or {}
            if applied.get("accepted"):
                changed = True
                original = applied.get("before") if original is None else original
                current = applied.get("after")
            att.update(code=res.code, message=res.message, status=rec.get("status"), outcome=rec.get("outcome"),
                       execution_id=res.execution_id, before=applied.get("before"), after=applied.get("after"),
                       verification=_short_verification(rec.get("verification")))
            attempts.append(att)
            self.store.event(key, "attempt", AGENT_ID, att)
            self.emit("auto_attempt", {**att, "stage": "done", "at": self.clock()})
            self.say("attempt", {**att, "stage": "done"})
            if rec.get("outcome") == "RESOLVED":
                resolved = att
                break
            if rec.get("status") == "uncertain" or res.code == "EXECUTION_STATE_UNCERTAIN":
                uncertain, stop_reason = True, "the state after a change is uncertain: never retried or reverted"
                break
            if not (res.ok and rec.get("status") == "completed"):
                stop_reason = f"attempt {n} stopped before a verified change: {res.message}"
                break
        else:
            stop_reason = f"the ladder is exhausted ({len(values)} attempt(s)) without a verified fix"
        if resolved is None and key in self._stop and not stop_reason.startswith("stopped"):
            stop_reason = f"stopped by {self._stop[key]}"

        if resolved:
            out = {"result": "resolved", "attempts": attempts, "resolved_by": resolved}
            self.store.event(key, "resolved", AGENT_ID, {"attempt": resolved["n"]})
            self.emit("auto_done", {**out, "at": self.clock()})
            self.say("resolved", out)
            return out

        revert = None
        if changed and not uncertain and key not in self._stop:
            revert = self._revert(key, plan, idx, original, current, attempts)
        stopped_by = self._stop.get(key)
        out = {"result": "handed_over", "attempts": attempts, "reason": stop_reason, "revert": revert,
               "component": comp, "stopped_by": stopped_by}
        if stopped_by:
            out["acknowledged_by"] = stopped_by        # pressing Stop is taking ownership
        else:
            self.store.lock(comp, key, stop_reason)
        self.store.event(key, "handover", AGENT_ID, out)
        self.emit("auto_done", {**out, "at": self.clock()})
        self.say("handover", out)
        return out

    def _revert(self, key, plan, idx, original, current, attempts) -> dict:
        """Put back the value from before the first attempt, through the executor, approved by this agent."""
        last = attempts[-1]
        rp = plan_rollback(plan, idx, {"before": original, "after": current}, last.get("outcome") or "NOT_RESOLVED",
                           f"automatic remediation did not resolve the incident after {len(attempts)} attempt(s)",
                           f"{key}{REVERT_KEY}")
        info = {"from": current, "to": original}
        if rp is None:
            info.update(ok=False, message="the value before the first attempt is unknown: nothing was reverted")
            self.emit("auto_revert", {**info, "stage": "done"})
            return info
        k = f"{key}{REVERT_KEY}"
        d = self.review.register_plan(k, rp.to_dict(), self.clock())
        dec = self.review.decide(k, d, 0, "approved", AGENT_ID,
                                 comment="revert after failed automatic remediation (policy: always revert)")
        if not dec.effective:
            info.update(ok=False, message=f"the revert could not be approved: {dec.message}")
            self.emit("auto_revert", {**info, "stage": "done"})
            return info
        self.emit("auto_revert", {**info, "stage": "started", "at": self.clock()})
        self.say("revert", {**info, "stage": "started"})
        res = self.execution.execute(ExecutionRequest(k, d, 0, AGENT_ID, self.clock()),
                                     progress=lambda stage, data: self.emit("auto_progress", {
                                         "attempt": "revert", "stage": stage, **_progress_data(stage, data)}))
        rec = (self.execution.store.get(res.execution_id) if res.execution_id else None) or {}
        applied = rec.get("applied") or {}
        info.update(ok=bool(applied.get("accepted")), message=res.message, status=rec.get("status"),
                    outcome=rec.get("outcome"), before=applied.get("before"), after=applied.get("after"),
                    verification=_short_verification(rec.get("verification")))
        self.store.event(key, "revert", AGENT_ID, info)
        self.emit("auto_revert", {**info, "stage": "done", "at": self.clock()})
        self.say("revert", {**info, "stage": "done"})
        return info

    def _progress(self, att: dict, stage: str, data: dict) -> None:
        self.emit("auto_progress", {"attempt": att["n"], "stage": stage, **_progress_data(stage, data)})
        if stage in ("checks_passed", "applied", "stopped"):
            rec = self.execution.store.get(data.get("execution_id")) or {}
            self.say("attempt", {**att, "stage": stage, "record": rec})


def _progress_data(stage: str, data: dict) -> dict:
    """The parts of an executor progress callback the page shows (JSON-safe)."""
    out = {"execution_id": data.get("execution_id")}
    if stage == "checks_passed":
        out.update(change=data.get("change"), checks=data.get("checks"))
    elif stage == "verifying":
        out.update(settle_max_s=data.get("settle_max_s"), window_s=data.get("window_s"))
    elif stage in ("applied", "stopped", "completed") and data.get("result") is not None:
        r = data["result"]
        out.update(code=r.code, message=r.message)
    return out


def _short_verification(v: dict | None) -> dict | None:
    if not v:
        return None
    return {"outcome": v.get("outcome"), "reason": v.get("reason"),
            "criteria": [{"statement": c.get("statement"), "status": c.get("status"),
                          "evidence": (c.get("evidence") or [""])[-1]} for c in (v.get("criteria") or [])]}
