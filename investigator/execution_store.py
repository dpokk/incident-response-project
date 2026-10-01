"""Durable execution audit and idempotency (Iteration 7), in the review database (state/reviews.db).

    plans (review.py) --digest--> decisions (review.py) --decision_id--> executions --> execution_attempts

`executions` holds at most ONE row per (incident, plan digest, action): the claim is taken atomically (a UNIQUE key)
before the live recheck, so a duplicate click, a retry or a second process can never reach the cluster writer twice.
A claim is never released: once taken, a further attempt on the same action of the same plan is refused, and acting
again requires a fresh investigation and plan. Every request, including refused and duplicate ones, is recorded in
`execution_attempts`.

An execution found in a non-terminal state on start-up was interrupted (crash, restart) while it may have been
changing the system: it is marked `uncertain`, never resumed and never retried.
"""
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

from .execution_model import Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY, incident_id TEXT, plan_digest TEXT, action_index INTEGER, action_type TEXT,
    kind TEXT, decision_id INTEGER, approver TEXT, approved_at REAL, executor TEXT, requested_at REAL,
    change_json TEXT, policy_json TEXT, recheck_json TEXT, dry_run_json TEXT, applied_json TEXT,
    verification_json TEXT, status TEXT, outcome TEXT, refusal TEXT, message TEXT, updated_at REAL,
    UNIQUE (incident_id, plan_digest, action_index));
CREATE TABLE IF NOT EXISTS execution_attempts (
    id INTEGER PRIMARY KEY, incident_id TEXT, plan_digest TEXT, action_index INTEGER, executor TEXT, at REAL,
    code TEXT, message TEXT, execution_id TEXT);
"""
JSON_FIELDS = ("change", "policy", "recheck", "dry_run", "applied", "verification")


def execution_id(incident_id: str, digest: str, index: int) -> str:
    """Deterministic identity of one action of one exact plan."""
    return hashlib.sha256(f"{incident_id}|{digest}|{index}".encode()).hexdigest()[:20]


class ExecutionStore:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    # ----------------------------------------------------------------------------- attempts (every request)
    def attempt(self, req, code: str, message: str, exec_id: str | None = None) -> None:
        with self._lock:
            self._db.execute("INSERT INTO execution_attempts (incident_id, plan_digest, action_index, executor, at, "
                             "code, message, execution_id) VALUES (?,?,?,?,?,?,?,?)",
                             (req.incident_id, req.plan_digest, req.action_index, req.executor, self.clock(), code,
                              message, exec_id))
            self._db.commit()

    def attempts(self, incident_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM execution_attempts WHERE incident_id=? ORDER BY id", (incident_id,)).fetchall()]

    # ----------------------------------------------------------------------------- the claim (idempotency)
    def claim(self, req, action_type: str, decision: dict, change: dict, policy: dict,
              kind: str = "remediation") -> tuple[bool, dict]:
        """Atomically claim one action of one plan. Returns (claimed, record); if it was already claimed, the
        existing record is returned untouched."""
        eid = execution_id(req.incident_id, req.plan_digest, req.action_index)
        now = self.clock()
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO executions (execution_id, incident_id, plan_digest, action_index, action_type, kind, "
                    "decision_id, approver, approved_at, executor, requested_at, change_json, policy_json, status, "
                    "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, req.incident_id, req.plan_digest, req.action_index, action_type, kind, decision.get("id"),
                     decision.get("reviewer"), decision.get("at"), req.executor, req.requested_at,
                     json.dumps(change), json.dumps(policy), Status.CLAIMED.value, now))
                self._db.commit()
                claimed = True
            except sqlite3.IntegrityError:
                self._db.rollback()
                claimed = False
        return claimed, self.get(eid)

    # ----------------------------------------------------------------------------- progress
    def update(self, eid: str, status: Status | None = None, **fields) -> dict:
        sets, args = ["updated_at=?"], [self.clock()]
        if status is not None:
            sets.append("status=?")
            args.append(status.value)
        for k, v in fields.items():
            if k in JSON_FIELDS:
                sets.append(f"{k}_json=?")
                args.append(json.dumps(v, default=str))
            elif k in ("outcome", "refusal", "message"):
                sets.append(f"{k}=?")
                args.append(v)
            else:
                raise KeyError(k)
        with self._lock:
            self._db.execute(f"UPDATE executions SET {', '.join(sets)} WHERE execution_id=?", (*args, eid))
            self._db.commit()
        return self.get(eid)

    def get(self, eid: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM executions WHERE execution_id=?", (eid,)).fetchone()
        return self._row(row) if row else None

    def for_action(self, incident_id: str, digest: str, index: int) -> dict | None:
        return self.get(execution_id(incident_id, digest, index))

    def executions(self, incident_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM executions WHERE incident_id=? ORDER BY requested_at",
                                    (incident_id,)).fetchall()
        return [self._row(r) for r in rows]

    def recover_interrupted(self) -> list[dict]:
        """Mark executions left in a non-terminal state (process stopped mid-way) as uncertain. Never resumes them."""
        live = [s.value for s in Status if not s.terminal]
        with self._lock:
            rows = self._db.execute(f"SELECT execution_id, status FROM executions WHERE status IN "
                                    f"({','.join('?' * len(live))})", live).fetchall()
        out = []
        for r in rows:
            if r["status"] == Status.VERIFYING.value:
                # The change is known to have been applied; only its verification was cut short.
                out.append(self.update(r["execution_id"], Status.COMPLETED, outcome="INCONCLUSIVE", message=(
                    "INCONCLUSIVE: verification was interrupted (process stopped); the change was applied, its effect "
                    "was not established. No further change is made")))
                continue
            out.append(self.update(r["execution_id"], Status.UNCERTAIN, message=(
                f"interrupted while '{r['status']}': whether the system was changed must be established by a fresh "
                f"investigation; not retried")))
        return out

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in JSON_FIELDS:
            raw = d.pop(f"{k}_json", None)
            d[k] = json.loads(raw) if raw else None
        return d
