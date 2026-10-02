"""Execution of approved remediation (Iteration 7): the typed vocabulary shared by the policy, the executor, the
audit store and the presentation. It imports nothing from the rest of the investigator.

An approval is permission to *consider* execution. Executing needs a separate, explicit request from an authorised
person, and passes, in order: authorization, approval validity (exact plan digest, plan still current), plan age,
policy, an idempotency claim, a fresh live-state recheck, a dry run, then exactly ONE typed change, then verification.

Changes are typed (`ChangeRequest`): there is no command string, no YAML and no generic patch anywhere.
"""
import re
from dataclasses import asdict, dataclass, field
from enum import Enum


class Refusal(str, Enum):
    """Why an execution request did not lead to a change."""
    UNAUTHORIZED = "EXECUTION_REFUSED_UNAUTHORIZED"
    UNKNOWN_PLAN = "EXECUTION_REFUSED_UNKNOWN_PLAN"
    SUPERSEDED = "EXECUTION_REFUSED_PLAN_SUPERSEDED"          # a newer plan exists: its approvals do not carry over
    NOT_APPROVED = "EXECUTION_REFUSED_NOT_APPROVED"
    UNSUPPORTED_ACTION = "EXECUTION_REFUSED_UNSUPPORTED_ACTION"
    MISSING_PARAMETER = "EXECUTION_REFUSED_MISSING_PARAMETER"
    STALE_PLAN = "EXECUTION_REFUSED_STALE_PLAN"
    POLICY = "EXECUTION_REFUSED_POLICY"
    ALREADY_COMPLETED = "EXECUTION_ALREADY_COMPLETED"
    IN_PROGRESS = "EXECUTION_IN_PROGRESS"
    UNCERTAIN = "EXECUTION_STATE_UNCERTAIN"                  # an earlier attempt was interrupted: never retried
    STATE_CHANGED = "EXECUTION_REFUSED_STATE_CHANGED"        # live state differs from the plan's assumption
    NOT_NEEDED = "EXECUTION_REFUSED_NOT_NEEDED"              # already applied / the condition no longer holds
    RECHECK_FAILED = "EXECUTION_REFUSED_RECHECK_FAILED"      # live state could not be established
    DRY_RUN_FAILED = "EXECUTION_REFUSED_DRY_RUN_FAILED"


class Status(str, Enum):
    """Lifecycle of one claimed execution (one action of one plan)."""
    CLAIMED = "claimed"
    REFUSED = "refused"                 # stopped by the recheck or the dry run; nothing was changed
    APPLYING = "applying"
    APPLY_FAILED = "apply_failed"       # the provider rejected the change
    VERIFYING = "verifying"
    COMPLETED = "completed"             # verification finished; see `outcome`
    UNCERTAIN = "uncertain"             # interrupted during/after a mutation or verification: stop, re-investigate

    @property
    def terminal(self) -> bool:
        return self in (Status.REFUSED, Status.APPLY_FAILED, Status.COMPLETED, Status.UNCERTAIN)


class Outcome(str, Enum):
    RESOLVED = "RESOLVED"               # change applied and every verification criterion held over the window
    NOT_RESOLVED = "NOT_RESOLVED"       # change applied, but a criterion failed (symptoms remain)
    INCONCLUSIVE = "INCONCLUSIVE"       # not enough evidence either way - never reported as success


# --------------------------------------------------------------------------- typed changes

@dataclass(frozen=True)
class ChangeRequest:
    """Exactly one typed change. `expected_*` is the pre-change value the plan assumed: the live recheck and the
    provider both refuse to act if the live value differs (compare-and-set), and treat the target value as
    already applied."""
    action_type: str                    # adjust_resource_limit | scale_workload | restore_configuration
    component: str                      # the workload changed (or restarted, for a configuration change)
    process: str | None = None          # adjust_resource_limit
    expected_bytes: float | None = None
    target_bytes: float | None = None
    expected_replicas: int | None = None   # scale_workload
    target_replicas: int | None = None
    source: str | None = None           # restore_configuration: e.g. "configmap/backend-config"
    item: str | None = None             # e.g. "DATABASE_URL"
    expected_host: str | None = None    # the host the plan saw (the faulty one)
    target_host: str | None = None      # host to restore (engineer-supplied) ...
    target_value: str | None = None     # ... or the whole recorded previous value
    expected_image: str | None = None   # rollback_release: the image the definition runs now (the bad release)
    target_image: str | None = None     # ... and the previous image, from recorded history

    def describe(self) -> str:
        if self.action_type == "adjust_resource_limit":
            return (f"memory limit of {self.component}/{self.process}: {mib(self.expected_bytes)} -> "
                    f"{mib(self.target_bytes)}")
        if self.action_type == "scale_workload":
            return f"replicas of {self.component}: {self.expected_replicas} -> {self.target_replicas}"
        if self.action_type == "rollback_release":
            return f"image of {self.component}/{self.process}: {self.expected_image} -> {self.target_image}"
        return (f"{self.item} in {self.source}: host '{self.expected_host}' -> "
                f"'{self.target_host or '(recorded previous value)'}', then restart {self.component}")

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class ExecutionRequest:
    incident_id: str
    plan_digest: str
    action_index: int
    executor: str                       # the person who clicked Execute (Slack user ID)
    requested_at: float


@dataclass
class PolicyResult:
    allowed: bool
    checks: list[dict] = field(default_factory=list)    # [{"check", "ok", "detail"}]

    @property
    def reasons(self) -> list[str]:
        return [c["detail"] for c in self.checks if not c["ok"]]


@dataclass
class ExecutionResult:
    """What one execution request produced (also what Slack shows)."""
    ok: bool                            # a change was applied (verification may still be running or failing)
    code: str                           # a Refusal value, or a Status value
    message: str
    execution_id: str | None = None
    checks: list[dict] = field(default_factory=list)
    record: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- quantities

_QUANTITY = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(Ki|Mi|Gi)?\s*$")
_UNITS = {None: 1, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30}


def memory_bytes(q) -> float | None:
    """'256Mi' -> 268435456.0; a number is taken as bytes; None/invalid -> None."""
    if q is None:
        return None
    if isinstance(q, (int, float)):
        return float(q)
    m = _QUANTITY.match(str(q))
    return float(m.group(1)) * _UNITS[m.group(2)] if m else None


def mib(b) -> str:
    if b is None:
        return "?"
    return f"{b / 2**30:g}Gi" if b >= 2**30 and b % 2**30 == 0 else f"{b / 2**20:g}Mi"
