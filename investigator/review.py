"""Human review of remediation plans (Iteration 6). Independent of Slack, and of any UI: Slack (or later a UI/API)
calls this module to record what an engineer decided; this module never calls back.

A decision is permanently bound to *exactly* what was reviewed: the incident, the plan's digest (a hash of the
whole RemediationPlan as presented), the action's index and type, and any value the engineer supplied.

Rules:
  * Only configured approvers can make an effective decision. Every attempt is recorded, effective or not.
  * Change actions can be approved, rejected or sent back for investigation; `investigate_further` can only be
    acknowledged - an approval never implies an investigation step is executable.
  * An action whose parameters are incomplete can only be approved with an engineer-supplied value, validated by
    its type. Candidate values in the plan are never used implicitly.
  * A newer plan for the same incident supersedes the older one: decisions on the old plan stay recorded but no
    longer apply, and decisions are only accepted against the current plan.
  * The first effective decision on an action stands; a repeat is a no-op, a different one is refused.

Nothing here executes anything. Approval is a durable record; acting on it is a later milestone (Iteration 7), which
must re-check live state and apply policy before any change.
"""
import hashlib
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

CHANGE_TYPES = {"adjust_resource_limit", "scale_workload", "restore_configuration"}
DECISIONS_FOR_CHANGE = {"approved", "rejected", "investigate_first"}
DECISIONS_FOR_INVESTIGATION = {"acknowledged"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    incident_id TEXT, plan_digest TEXT, plan_json TEXT, collected_at REAL, registered_at REAL,
    superseded_by TEXT, PRIMARY KEY (incident_id, plan_digest));
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY, incident_id TEXT, plan_digest TEXT, action_index INTEGER, action_type TEXT,
    decision TEXT, reviewer TEXT, at REAL, supplied_parameters TEXT, comment TEXT,
    effective INTEGER, reason TEXT);
CREATE INDEX IF NOT EXISTS decisions_by_plan ON decisions (incident_id, plan_digest, action_index);
"""


def plan_digest(plan: dict) -> str:
    """Deterministic SHA-256 of the exact plan (canonical JSON). Any change to the plan changes the digest."""
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- engineer-supplied parameters

_QUANTITY = re.compile(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi)$")
_UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30}
_HOST = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


class InvalidParameter(ValueError):
    pass


def _memory_limit(raw: str, action: dict):
    m = _QUANTITY.match(raw.strip())
    if not m:
        raise InvalidParameter("a memory quantity is required, e.g. 256Mi or 1Gi")
    value = float(m.group(1)) * _UNITS[m.group(2)]
    if value <= 0 or value > 64 * 2**30:
        raise InvalidParameter("the memory limit must be above 0 and at most 64Gi")
    current = action["parameters"].get("current_limit_bytes")
    if current and value <= current:
        raise InvalidParameter(f"the proposed limit must be higher than the current {action['parameters'].get('current_limit')}")
    return raw.strip()


def _replicas(raw: str, action: dict):
    if not raw.strip().isdigit():
        raise InvalidParameter("a whole number of replicas is required")
    n = int(raw.strip())
    if not 1 <= n <= 50:
        raise InvalidParameter("replicas must be between 1 and 50")
    if n == action["parameters"].get("current_replicas"):
        raise InvalidParameter("that is the current replica count")
    return n


def _hostname(raw: str, action: dict):
    host = raw.strip().lower()
    if not _HOST.match(host):
        raise InvalidParameter("a valid hostname is required, e.g. postgres")
    if host == str(action["parameters"].get("current_host", "")).lower():
        raise InvalidParameter("that is the host currently configured")
    return host


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    description: str
    parse: object


# What an engineer must supply per action type when the plan cannot (parameters_complete = false).
PARAMETER_SPECS = {
    "adjust_resource_limit": ParameterSpec("proposed_limit", "new memory limit (e.g. 256Mi)", _memory_limit),
    "scale_workload": ParameterSpec("target_replicas", "replica count (1-50)", _replicas),
    "restore_configuration": ParameterSpec("restore_to_host", "hostname to restore", _hostname),
}


# --------------------------------------------------------------------------- outcome

@dataclass
class ReviewOutcome:
    effective: bool
    status: str                         # the action's status after this attempt
    message: str                        # for the person who acted
    record: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- the service

class ReviewService:
    def __init__(self, path: Path, approvers: set[str] | list[str], clock=time.time):
        self.path, self.clock = Path(path), clock
        self.approvers = {a.strip() for a in approvers if a and a.strip()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _w(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.lastrowid

    # plans ---------------------------------------------------------------------------
    def register_plan(self, incident_id: str, plan: dict, collected_at: float | None = None) -> str:
        """Record the plan as presented for review. A different plan for the same incident supersedes the
        previous current one. Registering the same plan again is a no-op."""
        digest = plan_digest(plan)
        if self._q("SELECT 1 FROM plans WHERE incident_id=? AND plan_digest=?", (incident_id, digest)):
            return digest
        self._w("UPDATE plans SET superseded_by=? WHERE incident_id=? AND superseded_by IS NULL", (digest, incident_id))
        self._w("INSERT INTO plans VALUES (?,?,?,?,?,NULL)",
                (incident_id, digest, json.dumps(plan, sort_keys=True), collected_at, self.clock()))
        return digest

    def plan(self, incident_id: str, digest: str) -> dict | None:
        row = self._q("SELECT * FROM plans WHERE incident_id=? AND plan_digest=?", (incident_id, digest))
        if not row:
            return None
        r = row[0]
        return {"plan": json.loads(r["plan_json"]), "digest": digest, "collected_at": r["collected_at"],
                "registered_at": r["registered_at"], "superseded_by": r["superseded_by"]}

    def effective_decision(self, incident_id: str, digest: str, index: int) -> dict | None:
        """The decision that stands for one action of one plan (None = no effective decision yet)."""
        return self._effective(incident_id, digest, index)

    def current_digest(self, incident_id: str) -> str | None:
        row = self._q("SELECT plan_digest FROM plans WHERE incident_id=? AND superseded_by IS NULL", (incident_id,))
        return row[0]["plan_digest"] if row else None

    # decisions -------------------------------------------------------------------------
    def statuses(self, incident_id: str, digest: str) -> dict[int, dict]:
        """Per-action status for one plan: the effective decision, `superseded`, or `awaiting_review`."""
        rec = self.plan(incident_id, digest)
        if rec is None:
            return {}
        out = {}
        for i, _ in enumerate(rec["plan"]["actions"]):
            d = self._effective(incident_id, digest, i)
            if rec["superseded_by"]:
                out[i] = {"status": "superseded", **({k: d[k] for k in ("reviewer", "at")} if d else {})}
            elif d:
                out[i] = {"status": d["decision"], "reviewer": d["reviewer"], "at": d["at"],
                          "supplied_parameters": d["supplied_parameters"]}
            else:
                out[i] = {"status": "awaiting_review"}
        return out

    def decisions(self, incident_id: str, effective_only: bool = False) -> list[dict]:
        rows = self._q("SELECT * FROM decisions WHERE incident_id=?" + (" AND effective=1" if effective_only else "")
                       + " ORDER BY id", (incident_id,))
        return [self._row(r) for r in rows]

    def _effective(self, incident_id: str, digest: str, index: int) -> dict | None:
        row = self._q("SELECT * FROM decisions WHERE incident_id=? AND plan_digest=? AND action_index=? AND effective=1 "
                      "ORDER BY id LIMIT 1", (incident_id, digest, index))
        return self._row(row[0]) if row else None

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["supplied_parameters"] = json.loads(d["supplied_parameters"] or "{}")
        d["effective"] = bool(d["effective"])
        return d

    def decide(self, incident_id: str, digest: str, action_index: int, decision: str, reviewer: str,
               supplied: str | None = None, comment: str | None = None) -> ReviewOutcome:
        """Record a reviewer's decision on one action of one exact plan. Every attempt is recorded (with
        `effective` and a `reason`); only a valid one changes the action's status."""
        rec = self.plan(incident_id, digest)
        action = rec["plan"]["actions"][action_index] if rec and 0 <= action_index < len(rec["plan"]["actions"]) else None
        atype = action["type"] if action else None

        def refuse(reason: str, message: str) -> ReviewOutcome:
            # The audit keeps what was typed (unvalidated, never used); the decision stays ineffective.
            attempted = {"raw_input": str(supplied)[:200]} if supplied and str(supplied).strip() else {}
            self._record(incident_id, digest, action_index, atype, decision, reviewer, attempted, comment, False, reason)
            status = self.statuses(incident_id, digest).get(action_index, {}).get("status", "unknown") if rec else "unknown"
            return ReviewOutcome(False, status, message)

        if reviewer not in self.approvers:
            return refuse("unauthorized", "You are not an authorised approver; nothing was recorded as a decision.")
        if rec is None or action is None:
            return refuse("unknown_plan_or_action", "This plan or action is not known.")
        if rec["superseded_by"]:
            return refuse("superseded", "This plan has been superseded by a newer investigation; decide on the "
                                        "current plan instead.")
        allowed = DECISIONS_FOR_CHANGE if atype in CHANGE_TYPES else DECISIONS_FOR_INVESTIGATION
        if decision not in allowed:
            return refuse("decision_not_allowed", f"'{decision}' is not a valid decision for {atype}.")
        prior = self._effective(incident_id, digest, action_index)
        if prior:
            same = prior["decision"] == decision
            return refuse("duplicate" if same else "conflict",
                          f"Already {prior['decision']} by <@{prior['reviewer']}>; "
                          + ("nothing changed." if same else "a different decision was not recorded."))
        params = {}
        if decision == "approved" and not action["parameters_complete"]:
            spec = PARAMETER_SPECS.get(atype)
            if spec is None:
                return refuse("no_parameter_spec", "This action cannot be approved: its missing value has no input.")
            if not supplied or not str(supplied).strip():
                return refuse("parameter_missing", f"Approval needs the {spec.description}; the plan does not supply it.")
            try:
                params = {spec.name: spec.parse(str(supplied), action)}
            except InvalidParameter as exc:
                return refuse("parameter_invalid", f"Not recorded: {exc}.")
        elif supplied and str(supplied).strip() and decision == "approved":
            return refuse("parameter_not_expected", "This action's parameters are already complete; no value is taken.")
        record = self._record(incident_id, digest, action_index, atype, decision, reviewer, params, comment, True, "ok")
        return ReviewOutcome(True, decision, f"Recorded: {decision}. Nothing has been executed.", record)

    def _record(self, incident_id, digest, index, atype, decision, reviewer, params, comment, effective, reason) -> dict:
        at = self.clock()
        rid = self._w("INSERT INTO decisions (incident_id, plan_digest, action_index, action_type, decision, reviewer, "
                      "at, supplied_parameters, comment, effective, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (incident_id, digest, index, atype, decision, reviewer, at, json.dumps(params, default=str),
                       comment, int(effective), reason))
        return {"id": rid, "incident_id": incident_id, "plan_digest": digest, "action_index": index, "action_type": atype,
                "decision": decision, "reviewer": reviewer, "at": at, "supplied_parameters": params,
                "comment": comment, "effective": effective, "reason": reason}

    def summary(self, incident_id: str) -> str:
        digest = self.current_digest(incident_id)
        if not digest:
            return "no plan"
        st = self.statuses(incident_id, digest)
        rec = self.plan(incident_id, digest)
        counts: dict[str, int] = {}
        for i, a in enumerate(rec["plan"]["actions"]):
            counts[st[i]["status"]] = counts.get(st[i]["status"], 0) + 1
        return ", ".join(f"{n} {s.replace('_', ' ')}" for s, n in sorted(counts.items())) or "no actions"
