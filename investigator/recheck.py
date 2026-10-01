"""Fresh live-state recheck before any change (Iteration 7). Reads ONLY through the capability layer.

An approval was given on what the plan saw when its evidence was collected. Before acting, the live state must
still match that assumption. The semantics are the same for every action - the plan's pre-change value is the
precondition:

  live value == the plan's expected pre-change value   -> proceed (if the condition is still relevant)
  live value == the approved target value              -> NOT_NEEDED (already applied; never applied again)
  anything else, or the target no longer exists         -> STATE_CHANGED (fresh investigation required)
  the live state cannot be read                         -> RECHECK_FAILED (nothing is changed)

Per action:
  adjust_resource_limit  component and process exist; live memory limit == expected (e.g. 192Mi); the condition is
                         still relevant: the component is failing now, or it was killed at its memory limit within
                         the maximum plan age.
  scale_workload         component exists; live desired replicas == expected (e.g. 0).
  restore_configuration  the consumer still reads the item from the planned source; the host in the live value ==
                         the expected (faulty) host; the host to restore exists and has ready endpoints.

The recheck returns the concrete arguments for the compare-and-set write (e.g. the full configuration value read
just now), so the provider refuses if anything changes between this read and the write.
"""
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from .capabilities import TimeRange
from .execution_model import ChangeRequest, Refusal, memory_bytes, mib

RELEVANT_CAUSE = {"adjust_resource_limit": "memory_limit"}


@dataclass
class RecheckResult:
    ok: bool
    code: Refusal | None
    message: str
    checks: list[dict] = field(default_factory=list)     # [{"check", "ok", "detail"}] - shown and audited
    observed: dict = field(default_factory=dict)          # what the live state was (no sensitive values)
    write: dict = field(default_factory=dict)             # concrete compare-and-set arguments for the actuator

    def to_dict(self) -> dict:
        return {"ok": self.ok, "code": self.code.value if self.code else None, "message": self.message,
                "checks": self.checks, "observed": self.observed}


def recheck(caps, change: ChangeRequest, now: float, lookback_s: float, require_relevance: bool = True) -> RecheckResult:
    """`require_relevance`: also require that the condition the action addresses still holds (False for a rollback,
    whose purpose is to restore the previous value)."""
    fn = {"adjust_resource_limit": _memory_limit, "scale_workload": _replicas,
          "restore_configuration": _configuration}[change.action_type]
    try:
        return fn(caps, change, TimeRange(now - lookback_s, now), require_relevance)
    except Exception as exc:  # noqa: BLE001 - an unreadable state never leads to a change
        return RecheckResult(False, Refusal.RECHECK_FAILED, f"The live state could not be read "
                             f"({type(exc).__name__}); nothing was changed.")


class _Checks(list):
    def add(self, name: str, ok: bool, detail: str) -> bool:
        self.append({"check": name, "ok": ok, "detail": detail})
        return ok


def _refuse(code: Refusal, message: str, checks, observed) -> RecheckResult:
    return RecheckResult(False, code, message, list(checks), observed)


# --------------------------------------------------------------------------- adjust_resource_limit

def _memory_limit(caps, c: ChangeRequest, tr: TimeRange, require_relevance: bool) -> RecheckResult:
    checks, obs = _Checks(), {}
    state = caps.get_resource_state(c.component, tr)
    if state is None:
        if _absent(caps, c.component):
            checks.add("component_exists", False, f"{c.component} no longer exists")
            return _refuse(Refusal.STATE_CHANGED, f"{c.component} no longer exists: fresh investigation required.",
                           checks, obs)
        raise RuntimeError("resource state unavailable")
    checks.add("component_exists", True, f"{state.kind} {c.component} exists")
    limits = (state.limits or {}).get(c.process)
    if limits is None:
        checks.add("process_exists", False, f"process {c.process} is no longer in {c.component}")
        return _refuse(Refusal.STATE_CHANGED, f"{c.component} no longer runs {c.process}: fresh investigation "
                       f"required.", checks, obs)
    live = memory_bytes((limits or {}).get("memory"))
    obs.update(memory_limit=mib(live) if live else None, desired=state.desired, ready=state.ready)
    if live is not None and live == c.target_bytes:
        checks.add("current_value", False, f"memory limit is already {mib(live)} (the approved target)")
        return _refuse(Refusal.NOT_NEEDED, f"The memory limit of {c.component} is already {mib(live)}: already "
                       f"applied, not applied again.", checks, obs)
    if live != c.expected_bytes:
        checks.add("current_value", False, f"memory limit is {mib(live)}, the plan expected {mib(c.expected_bytes)}")
        return _refuse(Refusal.STATE_CHANGED, f"The memory limit of {c.component} is now {mib(live)}, not the "
                       f"{mib(c.expected_bytes)} the plan was based on: fresh investigation required.", checks, obs)
    checks.add("current_value", True, f"memory limit is still {mib(live)}, as the plan expected")
    if require_relevance:
        failing = state.desired > 0 and (state.ready < state.desired or any(not i.ready for i in state.instances)
                                         or any(p.waiting_cause == "restart_backoff"
                                                for i in state.instances for p in i.processes))
        recent = [t for t in _terminations(state) if t.cause == RELEVANT_CAUSE[c.action_type]
                  and (t.finished_at or 0) >= tr.start]
        obs.update(failing_now=failing, recent_limit_kills=len(recent))
        if not checks.add("still_relevant", failing or bool(recent),
                          f"{c.component}: {state.ready}/{state.desired} ready"
                          + (", failing now" if failing else "") + f"; {len(recent)} kill(s) at the memory limit "
                          f"in the last {(tr.end - tr.start) / 60:.0f} min"):
            return _refuse(Refusal.NOT_NEEDED, f"{c.component} is healthy and has not been killed at its memory "
                           f"limit recently: the condition no longer holds; fresh investigation required.", checks, obs)
    return RecheckResult(True, None, "live state matches the plan", list(checks), obs,
                         {"component": c.component, "process": c.process, "expected_bytes": live,
                          "new_bytes": c.target_bytes})


def _absent(caps, component: str) -> bool:
    comps = caps.list_components()
    if comps is None:                     # the read itself failed: unknown, not absent
        raise RuntimeError("component list unavailable")
    return component not in comps


def _terminations(state) -> list:
    out = [p.last_termination for i in state.instances for p in i.processes if p.last_termination]
    return out + [h.termination for h in state.history]


# --------------------------------------------------------------------------- scale_workload

def _replicas(caps, c: ChangeRequest, tr: TimeRange, require_relevance: bool) -> RecheckResult:
    checks, obs = _Checks(), {}
    state = caps.get_resource_state(c.component, tr)
    if state is None:
        if _absent(caps, c.component):
            checks.add("component_exists", False, f"{c.component} no longer exists")
            return _refuse(Refusal.STATE_CHANGED, f"{c.component} no longer exists: fresh investigation required.",
                           checks, obs)
        raise RuntimeError("resource state unavailable")
    checks.add("component_exists", True, f"{state.kind} {c.component} exists")
    obs.update(desired=state.desired, ready=state.ready)
    if state.desired == c.target_replicas:
        checks.add("current_value", False, f"{c.component} already has {state.desired} desired replica(s)")
        return _refuse(Refusal.NOT_NEEDED, f"{c.component} already has {state.desired} desired replica(s) (the "
                       f"approved target): already applied, not applied again.", checks, obs)
    if state.desired != c.expected_replicas:
        checks.add("current_value", False, f"{c.component} has {state.desired} desired replica(s), the plan "
                                           f"expected {c.expected_replicas}")
        return _refuse(Refusal.STATE_CHANGED, f"{c.component} now has {state.desired} desired replica(s), not the "
                       f"{c.expected_replicas} the plan was based on: fresh investigation required.", checks, obs)
    checks.add("current_value", True, f"{c.component} still has {state.desired} desired replica(s), as the plan "
                                      f"expected ({state.ready} ready)")
    return RecheckResult(True, None, "live state matches the plan", list(checks), obs,
                         {"component": c.component, "expected": state.desired, "new": c.target_replicas})


# --------------------------------------------------------------------------- restore_configuration

def host_of(value: str | None) -> str | None:
    if not value:
        return None
    if "://" in value:
        try:
            return urlsplit(value).hostname
        except ValueError:
            return None
    return value.strip().lower()


def replace_host(value: str, old: str, new: str) -> str | None:
    """The same value with only its host changed (URL or bare host); None if the format is not understood."""
    if "://" not in value:
        return new if value.strip().lower() == old.lower() else None
    parts = urlsplit(value)
    if (parts.hostname or "").lower() != old.lower():
        return None
    userinfo, _, hostport = parts.netloc.rpartition("@")
    rest = hostport[len(parts.hostname):] if hostport.lower().startswith(parts.hostname.lower()) else None
    if rest is None:
        return None
    return urlunsplit(parts._replace(netloc=(userinfo + "@" if userinfo else "") + new + rest))


def _configuration(caps, c: ChangeRequest, tr: TimeRange, require_relevance: bool) -> RecheckResult:
    checks, obs = _Checks(), {}
    entries = [e for e in caps.get_configuration(c.component) or [] if e.name == c.item]
    entry = next((e for e in entries if e.source == c.source), None)
    if entry is None:
        checks.add("reads_item", False, f"{c.component} no longer reads {c.item} from {c.source}")
        return _refuse(Refusal.STATE_CHANGED, f"{c.component} no longer reads {c.item} from {c.source}: fresh "
                       f"investigation required.", checks, obs)
    checks.add("reads_item", True, f"{c.component} reads {c.item} from {c.source}")
    if entry.sensitive or not c.source.startswith("configmap/"):
        checks.add("restorable_source", False, f"{c.source} is not a restorable configuration source")
        return _refuse(Refusal.STATE_CHANGED, f"{c.item} comes from {c.source}, which cannot be restored by this "
                       f"action.", checks, obs)
    live_host = host_of(entry.value)
    target_host = c.target_host or host_of(c.target_value)
    obs.update(live_host=live_host, target_host=target_host)
    if live_host and target_host and live_host == target_host.lower():
        checks.add("current_value", False, f"{c.item} already points to '{live_host}'")
        return _refuse(Refusal.NOT_NEEDED, f"{c.item} already points to '{live_host}' (the approved target): already "
                       f"applied, not applied again.", checks, obs)
    if live_host != (c.expected_host or "").lower():
        checks.add("current_value", False, f"{c.item} points to '{live_host}', the plan expected '{c.expected_host}'")
        return _refuse(Refusal.STATE_CHANGED, f"{c.item} now points to '{live_host}', not the '{c.expected_host}' the "
                       f"plan was based on: fresh investigation required.", checks, obs)
    checks.add("current_value", True, f"{c.item} still points to '{live_host}', as the plan expected")
    new_value = c.target_value or replace_host(entry.value, live_host, c.target_host)
    if not new_value or host_of(new_value) != target_host.lower():
        checks.add("new_value", False, f"the new value of {c.item} could not be formed")
        return _refuse(Refusal.STATE_CHANGED, f"The value of {c.item} has an unexpected format; nothing was changed.",
                       checks, obs)
    dep = next((d for d in caps.get_dependencies(c.component) or [] if d.variable == c.item), None)
    health = caps.get_service_health(target_host, dep.port if dep else None, dep.type if dep else "")
    obs.update(target_exists=health.exists, target_ready_endpoints=health.ready_endpoints)
    if not checks.add("target_available", bool(health.exists) and health.ready_endpoints > 0,
                      f"'{target_host}' " + ("exists with " + str(health.ready_endpoints) + " ready endpoint(s)"
                                             if health.exists else "does not exist")):
        return _refuse(Refusal.STATE_CHANGED, f"The host to restore, '{target_host}', is not available now: "
                       f"restoring it would not fix the dependency; fresh investigation required.", checks, obs)
    return RecheckResult(True, None, "live state matches the plan", list(checks), obs,
                         {"source": c.source, "item": c.item, "expected_value": entry.value, "new_value": new_value,
                          "restart_component": c.component})
