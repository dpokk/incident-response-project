"""Evidence collection: turns capability results into facts. Provider-independent.

    identify components -> resource state -> events -> logs -> services ->
    configuration & dependencies -> change history -> (optional) metrics

Every piece of evidence is requested through the capability layer (investigator.capabilities); this
module never talks to Kubernetes or any other provider directly. It records *facts* in an
EvidenceStore and draws no conclusions; diagnosis.py interprets them. It is never told what failure
was injected.
"""
from collections import defaultdict

from . import logparse
from .capabilities import Capabilities, TimeRange
from .capabilities.base import ResourceState, Termination
from .dependencies import check_dependency
from .evidence import EvidenceStore

CHANGE_LOOKBACK_S = 1800  # how far before the window a change may still explain an incident


def hms(t) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(t).astimezone().strftime("%H:%M:%S") if t else "?"


def mib(b) -> str:
    return f"{b / 2**20:.0f}Mi" if b else "n/a"


def collect(caps: Capabilities, incident: dict, start: float, end: float, entry: tuple | None = None,
            metrics_target: str = "frontend", log=print) -> EvidenceStore:
    store = caps.store
    tr = TimeRange(start, end)
    record_signals(store, incident)

    log("  [1/8] identifying components")
    store.step("identify_components", "all components in the investigated scope")
    components = caps.list_components() or []
    states = {c: caps.get_resource_state(c, tr) for c in components}

    log("  [2/8] resource state (components, instances, processes, termination history)")
    store.step("resource_state", "desired/ready replicas, instance readiness, process state, termination history")
    for c in components:
        if states[c]:
            record_resource_state(store, caps, states[c], start)

    log("  [3/8] events")
    store.step("events", "events for the components in the window; infrastructure events")
    record_events(store, caps, tr)

    log("  [4/8] application logs (current and previous process instances)")
    store.step("logs", "current logs since window start; previous-instance logs for restarted processes")
    for c in components:
        if states[c]:
            record_logs(store, caps, states[c], tr)

    log("  [5/8] services and endpoints")
    store.step("services", "services in scope and their ready endpoints")
    record_services(store, caps)

    log("  [6/8] configuration and dependencies")
    store.step("configuration", "effective configuration (secrets masked) -> dependency references")
    for c in components:
        record_dependencies(store, caps, c, start)

    log("  [7/8] change history (rollouts)")
    store.step("changes", "deployment history shortly before/during the incident")
    record_changes(store, caps, tr)

    if entry:
        record_entry_probe(store, caps, entry)

    if caps.metrics is not None:
        log("  [8/8] metrics (optional enrichment)")
        store.step("metrics", "request rate, error ratio and memory")
        from .metrics import metric_facts
        try:
            metric_facts(store, caps, incident, tr, list(states.values()), metrics_target)
        except Exception as exc:  # noqa: BLE001 - metrics are optional
            store.step("metrics", f"skipped: {exc}")
    else:
        store.step("metrics", "skipped: no metrics source configured")
    return store


# --------------------------------------------------------------------------- recorders (capability -> facts)

def record_signals(store: EvidenceStore, incident: dict) -> None:
    for sig in incident.get("signals", []):
        store.add("detector", sig.get("subject", "system"), "detection_signal", sig["text"], t=sig.get("t"),
                  signal=sig.get("kind"))


def record_resource_state(store: EvidenceStore, caps: Capabilities, rs: ResourceState, start: float) -> None:
    src = caps.resources.name
    subj = f"component/{rs.component}"
    store.add(f"{src}.resource_state", subj, "component_status",
              f"{rs.kind} {rs.scope}/{rs.component}: {rs.ready}/{rs.desired} replicas ready",
              component=rs.component, desired=rs.desired, ready=rs.ready, available=rs.available,
              instances=[i.name for i in rs.instances], limits=rs.limits)
    seen_terms = set()
    words = {}
    for inst in rs.instances:
        words[inst.name] = (inst.kind.lower(), inst.process_kind)
        store.add(f"{src}.resource_state", subj, "instance_status",
                  f"{inst.kind} {inst.name}: phase={inst.phase}, ready={inst.ready}, restarts={inst.restarts}",
                  instance=inst.name, phase=inst.phase, ready=inst.ready, restarts=inst.restarts,
                  unschedulable=inst.unschedulable, ready_since=inst.ready_since)
        for p in inst.processes:
            if p.state == "waiting" and p.waiting_reason:
                store.add(f"{src}.resource_state", subj, "process_waiting",
                          f"{inst.process_kind.capitalize()} {p.name} in {inst.kind.lower()} {inst.name} is waiting: "
                          f"{p.waiting_reason}" + (f" ({(p.waiting_message or '')[:160]})" if p.waiting_message else ""),
                          instance=inst.name, process=p.name, reason=p.waiting_reason, cause=p.waiting_cause,
                          message=p.waiting_message, problematic=p.waiting_cause is not None)
            t = p.last_termination
            if t and (t.finished_at or 0) >= start - 60:
                seen_terms.add((inst.name, p.name, round(t.finished_at or 0)))
                _termination(store, subj, inst.name, p.name, p.memory_limit_bytes, t, p.restarts,
                             f"{src}.resource_state", words[inst.name])
    for h in rs.history:  # earlier terminations the provider remembers (current state only keeps the latest)
        key = (h.instance, h.process, round(h.termination.finished_at))
        if any(k[0] == key[0] and k[1] == key[1] and abs(k[2] - key[2]) <= 2 for k in seen_terms):
            continue
        seen_terms.add(key)
        proc = next((p for i in rs.instances if i.name == h.instance for p in i.processes if p.name == h.process), None)
        _termination(store, subj, h.instance, h.process, proc.memory_limit_bytes if proc else None, h.termination,
                     h.restarts, h.source, words.get(h.instance, ("instance", "process")))


def _termination(store, subj, instance, process, mem, t: Termination, restarts, source, words):
    ran = (t.finished_at - t.started_at) if t.finished_at and t.started_at else None
    instance_word, process_word = words
    store.add(source, subj, "process_terminated",
              f"{process_word.capitalize()} {process} in {instance_word} {instance} terminated: reason={t.reason}, "
              f"exit code {t.exit_code}" + (f", after running {ran:.0f}s" if ran is not None else "")
              + (f" (memory limit {mib(mem)})" if mem else ""),
              t=t.finished_at, instance=instance, process=process, reason=t.reason, cause=t.cause,
              exit_code=t.exit_code, ran_s=ran, memory_limit=mem, restarts=restarts)


def record_events(store: EvidenceStore, caps: Capabilities, tr: TimeRange) -> None:
    src = caps.resources.name
    groups: dict[tuple, dict] = {}
    for e in caps.get_events(tr) or []:
        # Repeated health-check failures differ only in details after the first colon; group them together.
        key = (e.component, e.object_kind, e.reason,
               logparse.normalize(e.message.split(":")[0] if e.category == "health_check_failed" else e.message))
        g = groups.setdefault(key, {"component": e.component, "kind": e.object_kind, "reason": e.reason,
                                    "category": e.category, "type": e.type, "message": e.message[:300], "count": 0,
                                    "first": e.first, "last": e.last, "objects": set()})
        g["count"] += e.count
        g["first"] = min(filter(None, [g["first"], e.first]), default=None)
        g["last"] = max(filter(None, [g["last"], e.last]), default=None)
        g["objects"].add(e.object_name)
    start = tr.start
    for g in sorted(groups.values(), key=lambda g: g["first"] or 0):
        # Providers aggregate repeats into one record with first/last timestamps. If the first occurrence is
        # before the window (or unknown), say so: never move its start to the window edge - that would invent
        # a start time nobody observed.
        before = g["first"] is None or g["first"] < start
        when = (f"first seen {hms(g['first']) if g['first'] else 'at an unknown time'}, before the investigation "
                f"window; last seen {hms(g['last'])}; count includes earlier occurrences") if before \
            else f"{hms(g['first'])}-{hms(g['last'])}"
        store.add(f"{src}.events", f"component/{g['component']}", "event",
                  f"{g['type']} event {g['reason']} on {g['kind']} {', '.join(sorted(g['objects']))[:120]} "
                  f"(x{g['count']}, {when}): {g['message'][:200]}",
                  t=g["last"] if before else g["first"], t_basis="before_window" if before else "exact",
                  reason=g["reason"], category=g["category"], type=g["type"], count=g["count"], object_kind=g["kind"],
                  message=g["message"], first_seen=g["first"], observed_at=g["last"] if before else g["first"],
                  last=g["last"])
    for e in caps.get_events(tr, infrastructure=True) or []:
        if e.type == "Warning":
            store.add(f"{src}.events", f"{e.object_kind.lower()}/{e.object_name}", "infrastructure_event",
                      f"{e.object_kind} {e.object_name}: {e.reason}: {e.message[:200]}", t=e.first, reason=e.reason,
                      category=e.category)


def record_logs(store: EvidenceStore, caps: Capabilities, rs: ResourceState, tr: TimeRange,
                include_previous: bool | None = None) -> None:
    """Logs of every instance/process. include_previous: None = for processes that restarted (default),
    True/False = the caller (the planner) has decided."""
    start, end = tr.start, tr.end
    subj = f"component/{rs.component}"
    sigs: dict[tuple, dict] = {}
    levels = defaultdict(int)
    errors: dict[str, dict] = {}
    for inst in rs.instances:
        for p in inst.processes:
            generations = [("current", caps.get_logs(rs.component, inst.name, p.name, tr) or [])]
            if p.restarts > 0 and include_previous is not False:
                generations.append(("previous", caps.get_logs(rs.component, inst.name, p.name, tr, previous=True) or []))
            for generation, lines in generations:
                recs = [r for r in logparse.parse_records(lines) if r["_t"] is None or start - 5 <= r["_t"] <= end + 5]
                a = logparse.analyze(recs)
                for lvl, n in a["levels"].items():
                    levels[lvl] += n
                for s in a["signatures"]:
                    k = (s["signature"], s["target_host"], s["target_port"])
                    g = sigs.setdefault(k, {**s, "count": 0, "instances": set(), "generations": set()})
                    g["count"] += s["count"]
                    g["first"] = min(filter(None, [g["first"], s["first"]]), default=None)
                    g["last"] = max(filter(None, [g["last"], s["last"]]), default=None)
                    g["instances"].add(inst.name)
                    g["generations"].add(generation)
                for e in a["error_groups"]:
                    g = errors.setdefault(logparse.normalize(e["message"]), {**e, "count": 0})
                    g["count"] += e["count"]
                for tb in a["tracebacks"]:
                    site = tb["crash_site"] or {}
                    store.add("logs", subj, "log_exception",
                              f"{generation.capitalize()} {inst.process_kind} instance of {p.name} in "
                              f"{inst.kind.lower()} {inst.name} logged an unhandled {tb['type']}: {tb['message'][:160]}"
                              + (f" at {site.get('file')}:{site.get('line')} in {site.get('func')}()" if site else ""),
                              t=tb["t"], instance=inst.name, process=p.name, generation=generation,
                              exc_type=tb["type"], message=tb["message"], crash_site=site, frames=tb["frames"],
                              dependency_signature=logparse.classify(tb["type"] + ": " + tb["message"]))
                if generation == "previous" and a["tail"]:
                    store.add("logs", subj, "log_tail_before_exit",
                              f"Last log lines of the previous {p.name} instance in {inst.kind.lower()} {inst.name}: "
                              + " | ".join(t[:120] for t in a["tail"][-3:]),
                              t=a["last"], instance=inst.name, process=p.name, tail=a["tail"])
    for s in sorted(sigs.values(), key=lambda s: -s["count"]):
        target = f" referencing {s['target_host']}" + (f":{s['target_port']}" if s["target_port"] else "") if s["target_host"] else ""
        store.add("logs", subj, "log_signature",
                  f"{rs.component} logged {s['count']} {s['signature'].replace('_', ' ')} message(s){target} "
                  f"({hms(s['first'])}-{hms(s['last'])}), e.g. \"{s['sample'][:180]}\"",
                  t=s["first"], signature=s["signature"], target_host=s["target_host"], target_port=s["target_port"],
                  count=s["count"], instances=sorted(s["instances"]), generations=sorted(s["generations"]), last=s["last"],
                  sample=s["sample"])
    if levels.get("error") or levels.get("critical") or levels.get("warning"):
        top = sorted(errors.values(), key=lambda e: -e["count"])[:4]
        store.add("logs", subj, "log_levels",
                  f"{rs.component} logged {levels.get('error', 0) + levels.get('critical', 0)} error and "
                  f"{levels.get('warning', 0)} warning lines in the window"
                  + (f"; most frequent errors: " + "; ".join(f"\"{e['message'][:80]}\" x{e['count']}" for e in top) if top else ""),
                  errors=levels.get("error", 0) + levels.get("critical", 0), warnings=levels.get("warning", 0),
                  top_errors=[{"message": e["message"], "count": e["count"]} for e in top])


def record_services(store: EvidenceStore, caps: Capabilities) -> None:
    for s in caps.list_services() or []:
        store.add(f"{caps.resources.name}.endpoints", f"service/{s.name}", "service_state",
                  f"{s.kind} {s.scope}/{s.name} (ports {s.ports}): {s.ready_endpoints} ready, "
                  f"{s.not_ready_endpoints} not-ready endpoints",
                  service=s.name, ready=s.ready_endpoints, not_ready=s.not_ready_endpoints, selector=s.selector)


def record_dependencies(store: EvidenceStore, caps: Capabilities, component: str, start: float) -> list:
    refs = caps.get_dependencies(component) or []
    store.step("dependencies", f"{component}: {len(refs)} configured dependencies",
               refs=[f"{r.host}:{r.port}" for r in refs])
    return [check_dependency(caps, store, component, ref, start) for ref in refs]


def record_changes(store: EvidenceStore, caps: Capabilities, tr: TimeRange) -> None:
    for c in caps.get_deployment_history(TimeRange(tr.start - CHANGE_LOOKBACK_S, tr.end)) or []:
        store.add(f"{caps.resources.name}.deployment_history", f"component/{c.component}", "change",
                  f"{c.component_kind} {c.component} rolled out revision {c.revision} ({c.detail})",
                  t=c.at, component=c.component, change=c.kind, revision=c.revision)


def record_entry_probe(store: EvidenceStore, caps: Capabilities, entry: tuple) -> None:
    store.step("entry_probe", f"synthetic request to {entry[0]}:{entry[1]}{entry[2]}")
    res = caps.probe_request(*entry)
    status, body = (res.status, res.body) if res else (None, None)
    store.add("synthetic_probe", f"service/{entry[0]}", "entry_probe",
              f"Synthetic request GET {entry[2]} via service {entry[0]} returned HTTP {status}: "
              f"{(body or '').strip()[:120]}", status=status, body=body)
