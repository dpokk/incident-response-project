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
            record_resource_state(store, caps, states[c], start, end)

    log("  [3/8] events")
    store.step("events", "events for the components in the window; infrastructure events")
    record_events(store, caps, tr)

    log("  [4/8] application logs (current, previous and retained process runs)")
    store.step("logs", "current logs since window start; previous-instance logs for restarted processes; "
                       "retained logs of runs the live system no longer has")
    for c in components:
        if states[c]:
            record_logs(store, caps, states[c], tr, include_retained=True)

    log("  [5/8] services and endpoints")
    store.step("services", "services in scope and their ready endpoints")
    record_services(store, caps)

    log("  [6/8] configuration and dependencies")
    store.step("configuration", "effective configuration (secrets masked) -> dependency references")
    for c in components:
        record_dependencies(store, caps, c, start)

    log("  [7/8] change history (rollouts, recorded configuration and definition changes)")
    store.step("changes", "deployment and configuration history shortly before/during the incident")
    record_changes(store, caps, tr)
    for c in components:
        record_configuration_history(store, caps, c, tr)
    record_coverage(store, caps, tr)

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


def record_resource_state(store: EvidenceStore, caps: Capabilities, rs: ResourceState, start: float,
                          end: float | None = None) -> None:
    """end: terminations after the window are not part of this incident (matters when a past window is
    investigated: the live state then shows later events)."""
    end = float("inf") if end is None else end
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
                  unschedulable=inst.unschedulable, ready_since=inst.ready_since, created=inst.created)
        for p in inst.processes:
            if p.state == "waiting" and p.waiting_reason:
                store.add(f"{src}.resource_state", subj, "process_waiting",
                          f"{inst.process_kind.capitalize()} {p.name} in {inst.kind.lower()} {inst.name} is waiting: "
                          f"{p.waiting_reason}" + (f" ({(p.waiting_message or '')[:160]})" if p.waiting_message else ""),
                          instance=inst.name, process=p.name, reason=p.waiting_reason, cause=p.waiting_cause,
                          message=p.waiting_message, problematic=p.waiting_cause is not None)
            t = p.last_termination
            if t and start - 60 <= (t.finished_at or 0) <= end + 60:
                seen_terms.add((inst.name, p.name, round(t.finished_at or 0)))
                _termination(store, subj, inst.name, p.name, p.memory_limit_bytes, t, p.restarts,
                             f"{src}.resource_state", words[inst.name])
    for h in rs.history:  # earlier terminations the provider remembers (current state only keeps the latest)
        key = (h.instance, h.process, round(h.termination.finished_at))
        if any(k[0] == key[0] and k[1] == key[1] and abs(k[2] - key[2]) <= 2 for k in seen_terms):
            continue
        seen_terms.add(key)
        proc = next((p for i in rs.instances if i.name == h.instance for p in i.processes if p.name == h.process), None)
        default_words = next(iter(words.values()), ("instance", "process"))
        _termination(store, subj, h.instance, h.process, proc.memory_limit_bytes if proc else h.memory_limit_bytes,
                     h.termination, h.restarts, h.source, words.get(h.instance, default_words),
                     gone=h.instance_gone, logs_retained=h.logs_retained, generation=h.generation,
                     origin="retained" if h.source.endswith(".history") else "live")
    for p in rs.past_instances:   # instances that existed in the window but are gone now
        iw = p.kind.lower()
        store.add(f"{src}.history", subj, "past_instance",
                  f"{p.kind} {p.name} of {rs.component} no longer exists"
                  + (f" (gone since {hms(p.gone_at)})" if p.gone_at else " (its disappearance was not observed)")
                  + (f"; logs of {p.retained_runs} of its run(s) were retained" if p.retained_runs
                     else f"; no logs of this {iw} were retained"),
                  t=p.gone_at, t_basis="observed" if p.gone_at else "unknown", origin="retained",
                  instance=p.name, created=p.created, gone_at=p.gone_at, retained_runs=p.retained_runs)


def _termination(store, subj, instance, process, mem, t: Termination, restarts, source, words, gone=False,
                 logs_retained=False, generation=None, origin="live"):
    ran = (t.finished_at - t.started_at) if t.finished_at and t.started_at else None
    instance_word, process_word = words
    run = f" (run #{generation + 1})" if generation is not None else ""
    if gone and logs_retained:
        note = f"; that {instance_word} no longer exists, but the logs of this run were retained"
    elif gone:
        note = f"; that {instance_word} no longer exists, so its logs are unavailable"
    else:
        note = ""
    store.add(source, subj, "process_terminated",
              f"{process_word.capitalize()} {process} in {instance_word} {instance}{run} terminated: reason={t.reason}, "
              f"exit code {t.exit_code}" + (f", after running {ran:.0f}s" if ran is not None else "")
              + (f" (memory limit {mib(mem)})" if mem else "") + note,
              t=t.finished_at, origin=origin, instance=instance, process=process, reason=t.reason, cause=t.cause,
              exit_code=t.exit_code, ran_s=ran, memory_limit=mem, restarts=restarts, instance_gone=gone,
              logs_retained=logs_retained, generation=generation)


def record_events(store: EvidenceStore, caps: Capabilities, tr: TimeRange) -> None:
    src = caps.resources.name
    groups: dict[tuple, dict] = {}
    for e in caps.get_events(tr) or []:
        # Repeated health-check failures differ only in details after the first colon; group them together.
        key = (e.component, e.object_kind, e.reason,
               logparse.normalize(e.message.split(":")[0] if e.category == "health_check_failed" else e.message))
        g = groups.setdefault(key, {"component": e.component, "kind": e.object_kind, "reason": e.reason,
                                    "category": e.category, "type": e.type, "message": e.message[:300], "count": 0,
                                    "first": e.first, "last": e.last, "objects": set(), "origins": set()})
        g["origins"].add(getattr(e, "origin", "live"))
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
                  origin=_origin(g["origins"]), reason=g["reason"], category=g["category"], type=g["type"], count=g["count"], object_kind=g["kind"],
                  message=g["message"], first_seen=g["first"], observed_at=g["last"] if before else g["first"],
                  last=g["last"])
    for e in caps.get_events(tr, infrastructure=True) or []:
        if e.type == "Warning":
            store.add(f"{src}.events", f"{e.object_kind.lower()}/{e.object_name}", "infrastructure_event",
                      f"{e.object_kind} {e.object_name}: {e.reason}: {e.message[:200]}", t=e.first, reason=e.reason,
                      category=e.category)


def record_logs(store: EvidenceStore, caps: Capabilities, rs: ResourceState, tr: TimeRange,
                include_previous: bool | None = None, include_retained: bool = False) -> None:
    """Logs of every instance/process. include_previous: None = for processes that restarted (default),
    True/False = the caller (the planner) has decided. include_retained: also read the retained logs of runs
    the live system no longer has (earlier runs, instances that are gone)."""
    start, end = tr.start, tr.end
    subj = f"component/{rs.component}"
    # Every run whose logs are available: (label, instance, instance word, process word, process, lines, origin, ended)
    runs = []
    for inst in rs.instances:
        for p in inst.processes:
            runs.append(("current", inst.name, inst.kind.lower(), inst.process_kind, p.name,
                         caps.get_logs(rs.component, inst.name, p.name, tr) or [], "live", False))
            if p.restarts > 0 and include_previous is not False:
                runs.append(("previous", inst.name, inst.kind.lower(), inst.process_kind, p.name,
                             caps.get_logs(rs.component, inst.name, p.name, tr, previous=True) or [], "live", True))
    if include_retained:
        words = (rs.instances[0].kind.lower(), rs.instances[0].process_kind) if rs.instances else ("instance", "process")
        for h in caps.get_log_history(rs.component, tr) or []:
            runs.append((f"run #{h.generation + 1}", h.instance, words[0], words[1], h.process, h.lines, "retained",
                         h.termination is not None or h.instance_gone))
            if h.dropped:
                store.add(f"{caps.resources.name}.history", subj, "evidence_gap",
                          f"{h.dropped} log line(s) of {h.process} run #{h.generation + 1} in {words[0]} {h.instance} were "
                          f"not retained (recording rate limit); what they said is unknown",
                          origin="retained", instance=h.instance, process=h.process, generation=h.generation,
                          dropped=h.dropped, gap="log_lines_dropped")
    sigs: dict[tuple, dict] = {}
    levels = defaultdict(int)
    errors: dict[str, dict] = {}
    origins = set()
    for label, inst_name, inst_word, proc_word, proc, lines, origin, ended in runs:
        recs = [r for r in logparse.parse_records(lines) if r["_t"] is None or start - 5 <= r["_t"] <= end + 5]
        if recs:
            origins.add(origin)
        a = logparse.analyze(recs)
        for lvl, n in a["levels"].items():
            levels[lvl] += n
        for s in a["signatures"]:
            k = (s["signature"], s["target_host"], s["target_port"])
            g = sigs.setdefault(k, {**s, "count": 0, "instances": set(), "generations": set(), "origins": set(),
                                    "occurrences": []})
            g["occurrences"] += [(t, w, smp, inst_name, label, origin) for t, w, smp in s.get("occurrences", [])]
            g["count"] += s["count"]
            g["first"] = min(filter(None, [g["first"], s["first"]]), default=None)
            g["last"] = max(filter(None, [g["last"], s["last"]]), default=None)
            g["instances"].add(inst_name)
            g["generations"].add(label)
            g["origins"].add(origin)
        for e in a["error_groups"]:
            g = errors.setdefault(logparse.normalize(e["message"]), {**e, "count": 0})
            g["count"] += e["count"]
        run_name = (f"{label.capitalize()} {proc_word} instance of {proc}" if origin == "live"
                    else f"{proc_word.capitalize()} {proc} {label} (retained)")
        for tb in a["tracebacks"]:
            site = tb["crash_site"] or {}
            store.add("logs", subj, "log_exception",
                      f"{run_name} in {inst_word} {inst_name} logged an unhandled {tb['type']}: {tb['message'][:160]}"
                      + (f" at {site.get('file')}:{site.get('line')} in {site.get('func')}()" if site else ""),
                      t=tb["t"], origin=origin, instance=inst_name, process=proc, generation=label,
                      exc_type=tb["type"], message=tb["message"], crash_site=site, frames=tb["frames"],
                      dependency_signature=logparse.classify(tb["type"] + ": " + tb["message"]))
        if ended and a["tail"]:
            which = f"the previous {proc} instance" if origin == "live" else f"{proc} {label} (retained)"
            store.add("logs", subj, "log_tail_before_exit",
                      f"Last log lines of {which} in {inst_word} {inst_name}: " + " | ".join(t[:120] for t in a["tail"][-3:]),
                      t=a["last"], origin=origin, instance=inst_name, process=proc, generation=label, tail=a["tail"])
    for s in sorted((b for g in sigs.values() for b in _bursts(g)), key=lambda s: -s["count"]):
        target = f" referencing {s['target_host']}" + (f":{s['target_port']}" if s["target_port"] else "") if s["target_host"] else ""
        part = f" [burst {s['burst']} of {s['bursts']}]" if s["bursts"] > 1 else ""
        store.add("logs", subj, "log_signature",
                  f"{rs.component} logged {s['count']} {s['signature'].replace('_', ' ')} message(s){target} "
                  f"({hms(s['first'])}-{hms(s['last'])}){part}, e.g. \"{s['sample'][:180]}\""
                  + (" [includes retained logs]" if "retained" in s["origins"] else ""),
                  t=s["first"], origin=_origin(s["origins"]), signature=s["signature"], target_host=s["target_host"],
                  target_port=s["target_port"], count=s["count"], instances=sorted(s["instances"]),
                  generations=sorted(s["generations"]), last=s["last"], sample=s["sample"],
                  burst=s["burst"], bursts=s["bursts"])
    if levels.get("error") or levels.get("critical") or levels.get("warning"):
        top = sorted(errors.values(), key=lambda e: -e["count"])[:4]
        store.add("logs", subj, "log_levels",
                  f"{rs.component} logged {levels.get('error', 0) + levels.get('critical', 0)} error and "
                  f"{levels.get('warning', 0)} warning lines in the window"
                  + (f"; most frequent errors: " + "; ".join(f"\"{e['message'][:80]}\" x{e['count']}" for e in top) if top else ""),
                  origin=_origin(origins), t_basis="unknown",
                  errors=levels.get("error", 0) + levels.get("critical", 0), warnings=levels.get("warning", 0),
                  top_errors=[{"message": e["message"], "count": e["count"]} for e in top])


BURST_GAP_S = 120   # a signature silent this long, then back, is a separate burst (often a separate episode)


def _bursts(g: dict) -> list[dict]:
    """Split one signature's occurrences into bursts separated by silences of more than BURST_GAP_S. One window
    can hold two incidents with the same log message; a single first/last span would merge them."""
    occ = sorted((o for o in g["occurrences"] if o[0] is not None), key=lambda o: o[0])
    if not occ:
        return [{**g, "burst": 1, "bursts": 1}]
    groups, cur = [], [occ[0]]
    for o in occ[1:]:
        if o[0] - cur[-1][0] > BURST_GAP_S:
            groups.append(cur)
            cur = []
        cur.append(o)
    groups.append(cur)
    if len(groups) == 1:
        return [{**g, "burst": 1, "bursts": 1}]
    return [{**g, "count": sum(o[1] for o in grp), "first": grp[0][0], "last": grp[-1][0], "sample": grp[0][2],
             "instances": {o[3] for o in grp}, "generations": {o[4] for o in grp}, "origins": {o[5] for o in grp},
             "burst": n, "bursts": len(groups)} for n, grp in enumerate(groups, 1)]


def _origin(origins: set) -> str:
    return "mixed" if len(origins) > 1 else next(iter(origins), "live")


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


def record_configuration_history(store: EvidenceStore, caps: Capabilities, component: str, tr: TimeRange) -> None:
    """Recorded changes to what a component runs with. Only the time the source states is used as the change
    time; otherwise the change is bounded by the recorder's observations before and after it."""
    for c in caps.get_configuration_history(component, TimeRange(tr.start - CHANGE_LOOKBACK_S, tr.end)) or []:
        what = (f"{c.item} changed (value hidden)" if c.sensitive
                else f"{c.item} changed from {_val(c.before)} to {_val(c.after)}")
        if c.t is not None:
            when, kw = f"at {hms(c.t)}", {"t": c.t, "t_basis": "exact"}
        elif c.t_earliest is not None:
            when, kw = (f"between {hms(c.t_earliest)} and {hms(c.t_latest)}",
                        {"t": c.t_latest, "t_basis": "bounded", "t_earliest": c.t_earliest, "t_latest": c.t_latest})
        else:
            when, kw = (f"before {hms(c.t_latest)} (not observed earlier)",
                        {"t": c.t_latest, "t_basis": "observed", "t_latest": c.t_latest})
        store.add(f"{caps.resources.name}.history", f"component/{component}", "configuration_change",
                  f"{c.source}: {what}, {when}", origin="retained", component=component, source_object=c.source,
                  item=c.item, before=None if c.sensitive else c.before, after=None if c.sensitive else c.after,
                  sensitive=c.sensitive, **kw)


def _val(v) -> str:
    return "(unset)" if v is None else f"'{str(v)[:80]}'"


def record_coverage(store: EvidenceStore, caps: Capabilities, tr: TimeRange) -> None:
    """What evidence history exists for the window, and where it is missing. Missing history is stated as a
    fact so reports carry the limitation instead of silently reasoning from less."""
    spans = sorted(caps.get_evidence_coverage(tr) or [], key=lambda c: c.start)
    src = f"{caps.resources.name}.history"
    if not spans:
        store.add(src, "system", "evidence_gap",
                  f"No retained evidence history covers {hms(tr.start)}-{hms(tr.end)}: only current state, the current "
                  f"and previous run's logs, and events the platform still keeps could be examined",
                  t_basis="unknown", gap="no_history", window_start=tr.start, window_end=tr.end)
        return
    cursor = tr.start
    for c in spans:
        store.add(src, "system", "evidence_coverage",
                  f"Evidence history recorded {hms(max(c.start, tr.start))}-{hms(min(c.end, tr.end))} ({c.detail})",
                  t_basis="unknown", origin="retained", coverage_source=c.source, start=c.start, end=c.end)
        if c.start > cursor + 10:
            store.add(src, "system", "evidence_gap",
                      f"No evidence history for {hms(cursor)}-{hms(c.start)} (nothing was being recorded)",
                      t=c.start, t_basis="bounded", t_earliest=cursor, t_latest=c.start, gap="not_recorded",
                      start=cursor, end=c.start)
        cursor = max(cursor, c.end)
    if cursor < tr.end - 30:
        store.add(src, "system", "evidence_gap",
                  f"No evidence history for {hms(cursor)}-{hms(tr.end)} (recording stopped or fell behind)",
                  t=tr.end, t_basis="bounded", t_earliest=cursor, t_latest=tr.end, gap="not_recorded",
                  start=cursor, end=tr.end)


def record_entry_probe(store: EvidenceStore, caps: Capabilities, entry: tuple) -> None:
    store.step("entry_probe", f"synthetic request to {entry[0]}:{entry[1]}{entry[2]}")
    res = caps.probe_request(*entry)
    status, body = (res.status, res.body) if res else (None, None)
    store.add("synthetic_probe", f"service/{entry[0]}", "entry_probe",
              f"Synthetic request GET {entry[2]} via service {entry[0]} returned HTTP {status}: "
              f"{(body or '').strip()[:120]}", status=status, body=body)
