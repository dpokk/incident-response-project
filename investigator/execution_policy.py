"""Execution policy (Iteration 7): a deliberately small, configurable constraint on what may be executed, independent
of the planner. The planner proposes; the policy decides whether that kind of change is permitted here.

Loaded from config/execution_policy.json. This is environment/demo policy, not a policy engine: allowed action
types, allowed namespaces, a memory cap, a replica cap, the maximum plan age, who may execute, and the verification
window bounds.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .execution_model import ChangeRequest, PolicyResult, memory_bytes, mib

APPROVERS = "approvers"


def _snake(name: str) -> str:
    """AdjustResourceLimit -> adjust_resource_limit (both spellings are accepted in the file)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name.strip()).lower()


@dataclass(frozen=True)
class VerificationWindow:
    settle_max_s: float = 60
    window_s: float = 120
    window_min_s: float = 60
    window_max_s: float = 300

    def bounded(self, requested: float | None = None) -> float:
        return min(max(requested or self.window_s, self.window_min_s), self.window_max_s)


@dataclass(frozen=True)
class ExecutionPolicy:
    allowed_action_types: frozenset = frozenset({"adjust_resource_limit", "scale_workload", "restore_configuration"})
    allowed_namespaces: frozenset = frozenset({"shop"})
    max_memory_bytes: float = 2**30
    max_replicas: int = 3
    max_plan_age_s: float = 900
    allowed_executors: frozenset | str = APPROVERS    # "approvers" = the review approvers allowlist
    verification: VerificationWindow = field(default_factory=VerificationWindow)

    @classmethod
    def load(cls, path: Path) -> "ExecutionPolicy":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        v = d.get("verification") or {}
        execs = d.get("allowed_executors", APPROVERS)
        mem = memory_bytes(d.get("max_memory_limit", "1Gi"))
        if mem is None:
            raise ValueError(f"execution policy: invalid max_memory_limit {d.get('max_memory_limit')!r}")
        window = VerificationWindow(float(v.get("settle_max_seconds", 60)), float(v.get("window_seconds", 120)),
                                    float(v.get("window_min_seconds", 60)), float(v.get("window_max_seconds", 300)))
        if not window.window_min_s <= window.window_s <= window.window_max_s:
            raise ValueError("execution policy: verification window_seconds must lie within its min/max")
        return cls(allowed_action_types=frozenset(_snake(a) for a in d.get("allowed_action_types", [])),
                   allowed_namespaces=frozenset(d.get("allowed_namespaces", [])),
                   max_memory_bytes=mem, max_replicas=int(d.get("max_replicas", 3)),
                   max_plan_age_s=float(d.get("max_plan_age_seconds", 900)),
                   allowed_executors=APPROVERS if execs == APPROVERS else frozenset(execs),
                   verification=window)

    # ----------------------------------------------------------------------------- authorization
    def executors(self, approvers) -> frozenset:
        """Who may execute. Today the same allowlist as approval; a separate list can be configured."""
        return frozenset(approvers) if self.allowed_executors == APPROVERS else frozenset(self.allowed_executors)

    def may_execute(self, user: str, approvers) -> bool:
        return bool(user) and user in self.executors(approvers)

    # ----------------------------------------------------------------------------- freshness
    def plan_age_ok(self, collected_at: float | None, now: float) -> tuple[bool, str]:
        if not collected_at:
            return False, "the plan has no evidence collection time, so its age cannot be established"
        age = now - collected_at
        if age > self.max_plan_age_s:
            return False, (f"the plan is {age / 60:.0f} min old (maximum {self.max_plan_age_s / 60:.0f} min): "
                           f"fresh investigation required")
        return True, f"plan age {age / 60:.1f} min (maximum {self.max_plan_age_s / 60:.0f} min)"

    # ----------------------------------------------------------------------------- the change itself
    def check(self, change: ChangeRequest, namespace: str) -> PolicyResult:
        checks = [
            {"check": "action_type", "ok": change.action_type in self.allowed_action_types,
             "detail": f"action type {change.action_type} " + ("is allowed" if change.action_type in
                                                               self.allowed_action_types else "is not allowed")},
            {"check": "namespace", "ok": namespace in self.allowed_namespaces,
             "detail": f"namespace '{namespace}' " + ("is allowed" if namespace in self.allowed_namespaces
                                                      else "is not allowed")},
        ]
        if change.action_type == "adjust_resource_limit":
            ok = change.target_bytes is not None and 0 < change.target_bytes <= self.max_memory_bytes
            checks.append({"check": "max_memory_limit", "ok": ok,
                           "detail": f"memory limit {mib(change.target_bytes)} "
                                     + ("within" if ok else "exceeds") + f" the maximum {mib(self.max_memory_bytes)}"})
        if change.action_type == "scale_workload":
            ok = change.target_replicas is not None and 0 <= change.target_replicas <= self.max_replicas
            checks.append({"check": "max_replicas", "ok": ok,
                           "detail": f"{change.target_replicas} replicas "
                                     + ("within" if ok else "exceeds") + f" the maximum {self.max_replicas}"})
        return PolicyResult(all(c["ok"] for c in checks), checks)
