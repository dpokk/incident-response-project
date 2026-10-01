"""RemediationPlan: the structured contract between remediation planning and anything that presents it (Iteration 5).

Slack, a web UI or an API consume this object (via `to_dict()`); they do not define it, and the planner does not
know about them. It deliberately imports nothing from the rest of the investigator.

A plan is a *proposal for a human*. It has no execution path:
  * `requires_human_approval` is always True;
  * `execution.status` is always "not_executed" in this iteration;
  * actions are typed (ActionType) with typed parameters - there is no command string anywhere.

Stability: `SCHEMA_VERSION` changes on any incompatible change. Enum values are lowercase strings; lists keep a
deterministic order; `to_dict()` output is plain JSON types.
"""
from dataclasses import asdict, dataclass, field
from enum import Enum

SCHEMA_VERSION = "1"


class IncidentState(str, Enum):
    ACTIVE = "active"                  # the failure was still present when the evidence was collected
    RECOVERED = "recovered"            # the failing components had recovered by then
    UNKNOWN = "unknown"


class Assessment(str, Enum):
    ACTION_PROPOSED = "action_proposed"            # at least one action addresses a condition that holds now
    NO_IMMEDIATE_ACTION = "no_immediate_action"    # nothing to fix now (possibly preventive actions / follow-up)
    INVESTIGATE_FURTHER = "investigate_further"    # the evidence does not support a corrective action
    NO_SAFE_ACTION = "no_safe_action"              # the failure is current, but no supported typed action fits


class ActionType(str, Enum):
    ADJUST_RESOURCE_LIMIT = "adjust_resource_limit"
    SCALE_WORKLOAD = "scale_workload"
    RESTORE_CONFIGURATION = "restore_configuration"
    INVESTIGATE_FURTHER = "investigate_further"    # never executable: tells a human what to examine and why


class Urgency(str, Enum):
    IMMEDIATE = "immediate"            # the condition it addresses holds now
    PREVENTIVE = "preventive"          # the condition held during the incident, not now; prevents recurrence
    FOLLOW_UP = "follow_up"            # investigation, no change


class Check(str, Enum):
    """Verification checks, named after what the existing capabilities can observe (evaluated in Iteration 7)."""
    COMPONENT_READY = "component_ready"                  # get_resource_state: ready == desired
    NO_NEW_TERMINATIONS = "no_new_terminations"          # get_resource_state: no new terminations of a cause
    DEPENDENCY_AVAILABLE = "dependency_available"        # get_service_health / check_connectivity
    NO_DEPENDENCY_ERRORS = "no_dependency_errors"        # get_logs: no new connection errors for an endpoint
    ENTRY_REQUESTS_SUCCEED = "entry_requests_succeed"    # probe_request
    ERROR_RATIO_BELOW = "error_ratio_below"              # get_metrics (when a metrics provider exists)


@dataclass
class Statement:
    """A sentence and the fact IDs (from the incident's evidence) it rests on."""
    statement: str
    fact_ids: list[str] = field(default_factory=list)


@dataclass
class VerificationCriterion:
    check: Check
    subject: str                       # component or endpoint
    expectation: dict                  # machine-readable, e.g. {"cause": "memory_limit", "count": 0}
    statement: str


@dataclass
class Rollback:
    strategy: str                      # e.g. "restore_previous_value", "none_needed"
    statement: str
    restores: dict = field(default_factory=dict)   # what would be restored (values only when known and not sensitive)


@dataclass
class ProposedAction:
    type: ActionType
    target: dict                       # {"component": ...} or {"config_item": ..., "source": ...}
    parameters: dict                   # typed per action type; None = not determinable from the evidence
    parameters_complete: bool          # False: a human must supply what the evidence cannot (e.g. a new limit)
    urgency: Urgency
    summary: str
    rationale: list[Statement] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    expected_final_state: list[str] = field(default_factory=list)
    risks: list[Statement] = field(default_factory=list)
    rollback: Rollback | None = None
    verification: list[VerificationCriterion] = field(default_factory=list)


@dataclass
class Uncertainty:
    confidence: float                  # copied from the diagnosis - the planner computes no confidence of its own
    confidence_label: str
    competing_causes: list[dict] = field(default_factory=list)   # unexplained alternatives the evidence also supports
    evidence_gaps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class RemediationPlan:
    incident_id: str
    diagnosis: dict                    # summary copied from the diagnosis (never re-derived)
    incident_state: IncidentState
    current_state: list[Statement]
    assessment: Assessment
    assessment_reason: str
    actions: list[ProposedAction]
    rationale: list[Statement]
    uncertainty: Uncertainty
    evidence: list[dict]               # [{"id", "text"}] snapshot of every fact the plan cites
    requires_human_approval: bool = True
    approval: dict = field(default_factory=lambda: {"status": "awaiting_review"})        # Iteration 6
    execution: dict = field(default_factory=lambda: {"status": "not_executed", "executable": False})
    schema_version: str = SCHEMA_VERSION
    generated_by: str = "deterministic remediation planner (rule-based; plans only, never executes)"

    def to_dict(self) -> dict:
        return _plain(asdict(self))


def _plain(v):
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v
