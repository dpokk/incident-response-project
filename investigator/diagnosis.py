"""Diagnosis: interpret collected facts into a failure category, affected component and root cause.

How it works (and why it is not scenario-based):
  * Every investigated component is evaluated against a general failure taxonomy: memory
    exhaustion, application crash, dependency misconfiguration, dependency unavailable, image pull
    failure, container configuration error, unschedulable, health-check failure. Each check is a
    function of *observed facts* (termination reasons, exit codes, tracebacks, log signatures,
    service/endpoint state, connectivity probes, config changes) and returns a score, the facts
    that support it and the facts that argue against it.
  * Findings are followed along the dependency graph (built from configuration): if a consumer's
    failures are explained by a dependency component that has its own intrinsic failure, the root
    is the dependency and the consumer is recorded as impacted.
  * The best-supported finding becomes the diagnosis; the others are reported with why they were
    rejected. Every statement cites fact IDs.
Nothing here knows which failure was injected, and nothing here changes the system.
"""
from dataclasses import dataclass, field

from .evidence import EvidenceStore, Fact
from .logparse import DEPENDENCY_SIGNATURES

CATEGORY_LABELS = {
    "memory_exhaustion": "Memory exhaustion (killed at its memory limit)",
    "application_crash": "Application crash (unhandled exception)",
    "dependency_misconfiguration": "Dependency configuration / connectivity error",
    "dependency_unavailable": "Dependency unavailable",
    "image_pull_failure": "Image unavailable",
    "container_config_error": "Invalid process configuration",
    "unschedulable": "Instance cannot be scheduled",
    "health_check_failure": "Health check failure",
    "undetermined": "Undetermined (insufficient evidence)",
}


@dataclass
class Finding:
    category: str
    component: str
    score: float = 0.0
    support: list = field(default_factory=list)       # [(fact_id, why it supports)]
    against: list = field(default_factory=list)       # [(fact_id or None, why it argues against)]
    reasoning: list = field(default_factory=list)     # [{"statement": ..., "facts": [...]}]
    root_cause: str = ""
    dependency: dict | None = None
    contributing: list = field(default_factory=list)
    impacted: list = field(default_factory=list)
    backing: list = field(default_factory=list)       # components behind a dependency service
    explained_by: str = ""                            # set when this is an effect of another finding

    def say(self, statement: str, facts: list[Fact] | list[str]) -> None:
        ids = [f.id if isinstance(f, Fact) else f for f in facts if f]
        self.reasoning.append({"statement": statement, "facts": ids})

    def add(self, weight: float, facts, why: str) -> None:
        self.score += weight
        for f in facts if isinstance(facts, list) else [facts]:
            if f:
                self.support.append((f.id if isinstance(f, Fact) else f, why))

    def reject(self, why: str, fact: Fact | None = None) -> None:
        self.against.append((fact.id if fact else None, why))


def _reasons(facts: list[Fact]) -> str:
    """The provider's own words for these terminations, for display (e.g. "OOMKilled, exit code 137")."""
    pairs = sorted({(f.data.get("reason"), f.data.get("exit_code")) for f in facts}, key=str)
    return "; ".join(f"reason {r}, exit code {c}" for r, c in pairs)


# --------------------------------------------------------------------------- component views

class View:
    """Facts about one component, grouped for the checks."""

    def __init__(self, store: EvidenceStore, name: str):
        self.name = name
        subj = f"component/{name}"
        f = [x for x in store.facts if x.subject == subj]
        self.status = next((x for x in f if x.kind == "component_status"), None)
        self.instances = [x for x in f if x.kind == "instance_status"]
        self.terms = [x for x in f if x.kind == "process_terminated"]
        self.waiting = [x for x in f if x.kind == "process_waiting"]
        self.events = [x for x in f if x.kind == "event"]
        self.sigs = [x for x in f if x.kind == "log_signature"]
        self.exceptions = [x for x in f if x.kind == "log_exception"]
        self.tails = [x for x in f if x.kind == "log_tail_before_exit"]
        self.levels = next((x for x in f if x.kind == "log_levels"), None)
        self.refs = [x for x in f if x.kind == "config_reference"]
        self.changes = [x for x in f if x.kind == "change"]
        self.metrics_mem = [x for x in f if x.kind == "metric_memory_high"]

    def terms_by(self, *causes: str) -> list[Fact]:
        return [t for t in self.terms if t.data.get("cause") in causes]

    def events_by(self, *categories: str) -> list[Fact]:
        return [e for e in self.events if e.data.get("category") in categories]

    @property
    def error_lines(self) -> int:
        return self.levels.data["errors"] if self.levels else 0

    def symptomatic(self) -> bool:
        return bool(self.terms or [w for w in self.waiting if w.data["problematic"]]
                    or [p for p in self.instances if not p.data["ready"]] or self.error_lines > 0
                    or [e for e in self.events if e.data["type"] == "Warning"])

    def symptom_facts(self) -> list[Fact]:
        out = list(self.terms[:3]) + [w for w in self.waiting if w.data["problematic"]][:2]
        out += [p for p in self.instances if not p.data["ready"]][:2]
        out += [e for e in self.events if e.data["type"] == "Warning"][:3]
        if self.levels and self.error_lines:
            out.append(self.levels)
        return out


# --------------------------------------------------------------------------- checks

def check_memory(v: View, store: EvidenceStore) -> Finding:
    f = Finding("memory_exhaustion", v.name)
    oom = v.terms_by("memory_limit")
    killed = v.terms_by("killed")
    mem_logs = [s for s in v.sigs if s.data["signature"] == "memory_pressure"]
    if oom:
        insts = sorted({t.data["instance"] for t in oom})
        f.add(0.6, oom, "terminated for exceeding the memory limit")
        f.say(f"{len(oom)} termination(s) in {len(insts)} {v.name} instance(s) were kills at the memory limit "
              f"({_reasons(oom)}): the process exceeded the memory it is allowed to use", oom)
    elif killed:
        f.add(0.2, killed, "killed from outside without a memory-limit reason")
    else:
        f.reject("no termination was a kill at the memory limit or an external kill")
    if mem_logs:
        f.add(0.15 if (oom or killed) else 0.05, mem_logs, "application logged memory pressure")
        f.say(f"Before termination {v.name} logged that memory usage was approaching its limit", mem_logs)
    if v.metrics_mem:
        f.add(0.15 if (oom or killed) else 0.05, v.metrics_mem, "metrics show memory close to the limit")
        f.say(f"Metrics show memory working set reaching {max(m.data['peak_ratio'] for m in v.metrics_mem):.0%} of the limit",
              v.metrics_mem)
    if len(oom) > 1:
        f.add(0.1, oom[1:3], "repeated memory-limit kills")
    limit = next((t.data["memory_limit"] for t in oom if t.data.get("memory_limit")), None)
    traffic = store.find(kind="metric_traffic_change")
    if traffic and (oom or killed):
        tr = traffic[0]
        f.contributing.append({"statement": f"Incoming traffic rose from ~{tr.data['baseline']:.0f} to ~{tr.data['peak']:.0f} "
                               f"req/s ({tr.data['peak'] / tr.data['baseline']:.1f}x) shortly before the terminations",
                               "facts": [tr.id]})
    shown = next((t.data.get("reason") for t in oom if t.data.get("reason")), None)
    f.root_cause = (f"{v.name} exceeded its memory limit" + (f" ({limit / 2**20:.0f}Mi)" if limit else "")
                    + " and was killed" + (f" ({shown})" if shown else "")
                    + (f"; this followed a {traffic[0].data['peak'] / traffic[0].data['baseline']:.1f}x increase in "
                       f"incoming traffic" if traffic else ""))
    f.score = min(f.score, 1.0)
    return f


def check_crash(v: View, store: EvidenceStore, dep_health: dict) -> Finding:
    f = Finding("application_crash", v.name)
    errored = v.terms_by("error_exit")
    oom = v.terms_by("memory_limit")
    backoff = [w for w in v.waiting if w.data.get("cause") == "restart_backoff"] + v.events_by("restart_backoff")
    app_exc = [e for e in v.exceptions if not e.data.get("dependency_signature")]
    dep_exc = [e for e in v.exceptions if e.data.get("dependency_signature")]
    if not errored:
        f.reject("no process exited on its own with an error")
        if oom:
            f.reject("terminations were kills at the memory limit, not exits by the application", oom[0])
        return f
    codes = sorted({t.data["exit_code"] for t in errored if t.data.get("exit_code") is not None}, key=str)
    f.add(0.35, errored, "process exited on its own with an error")
    f.say(f"{len(errored)} {v.name} termination(s) were exits by the process itself ({_reasons(errored)}): "
          f"it was not killed for memory or by an outside signal", errored)
    if app_exc:
        e = sorted(app_exc, key=lambda x: x.data["generation"] != "previous")[0]
        site = e.data.get("crash_site") or {}
        f.add(0.3, app_exc[:3], "unhandled exception in the application's logs")
        f.say(f"The terminated instance's logs end with an unhandled {e.data['exc_type']}: {e.data['message'][:120]}"
              + (f", raised in {site.get('func')}() at {site.get('file')}:{site.get('line')}" if site else ""), app_exc[:3])
    elif dep_exc:
        f.score *= 0.5
        f.reject("the exception that ended the process is a dependency connection error, pointing at the dependency",
                 dep_exc[0])
    else:
        f.reject("no exception/traceback was found in the logs of the terminated instances")
    if backoff:
        f.add(0.15, backoff[:3], "restarts are being backed off (crash loop)")
        f.say("The platform keeps restarting the process and is now backing off between attempts: the failure recurs "
              "on every start", backoff[:3])
    elif len(errored) > 1:
        f.add(0.1, errored[1:3], "repeated crashes")
    runtimes = [t.data["ran_s"] for t in errored if t.data.get("ran_s") is not None]
    if runtimes:
        f.say(f"Each instance ran only {min(runtimes):.0f}-{max(runtimes):.0f}s before exiting", errored[:2])
    healthy_deps = [d for d in dep_health.get(v.name, []) if d["healthy"]]
    if dep_health.get(v.name) and len(healthy_deps) == len(dep_health[v.name]):
        f.add(0.1, [d["fact"] for d in healthy_deps], "its dependencies are reachable, so the crash is not a dependency outage")
        f.say(f"{v.name}'s configured dependencies are reachable ({', '.join(d['endpoint'] for d in healthy_deps)}), so "
              f"the crash is not caused by a dependency being down", [d["fact"] for d in healthy_deps])
    if oom:
        f.reject(f"{len(oom)} other termination(s) were kills at the memory limit", oom[0])
    code = codes[0] if codes else "unknown"
    if app_exc:
        e = sorted(app_exc, key=lambda x: x.data["generation"] != "previous")[0]
        site = e.data.get("crash_site") or {}
        f.root_cause = (f"{v.name} crashes with an unhandled {e.data['exc_type']} ({e.data['message'][:100]})"
                        + (f" in {site.get('func')}() at {site.get('file')}:{site.get('line')}" if site else "")
                        + f"; the process exits with code {code} and is restarted repeatedly")
    else:
        f.root_cause = f"{v.name} process exits with code {code} on its own and is restarted repeatedly"
    f.score = min(f.score, 1.0)
    return f


def check_dependencies(v: View, store: EvidenceStore) -> list[Finding]:
    out = []
    for ref in v.refs:
        host, port = ref.data["host"], ref.data["port"]
        subj = f"dependency/{host}:{port}"
        dep = {"host": host, "port": port, "type": ref.data["dep_type"], "variable": ref.data["variable"],
               "source": ref.data["config_source"], "endpoint": f"{host}:{port}"}
        dep_logs = [s for s in v.sigs if s.data["signature"] in DEPENDENCY_SIGNATURES and _matches(s, host, port)]
        dep_exc = [e for e in v.exceptions if e.data.get("dependency_signature") and host in e.data["message"]]
        lookup = next(iter(store.find(kind="service_lookup", subject=subj)), None)
        eps = next(iter(store.find(kind="service_endpoints", subject=subj)), None)
        backing = store.find(kind="backing_component", subject=subj)
        probe = next((p for p in store.find(kind="connectivity_probe", subject=subj) if not p.data.get("alternative_for")), None)
        mismatch = next(iter(store.find(kind="service_port_mismatch", subject=subj)), None)
        similar = [s for s in store.find(kind="similar_service", subject=subj) if s.data["ready"] > 0]
        alt_ok = [p for p in store.find(kind="connectivity_probe") if p.data.get("alternative_for") == host and p.data["tcp"] == "ok"]
        changed = [c for c in store.find(kind="config_changed") if c.data["variable"] == ref.data["variable"]
                   and c.data["consumer"] == v.name]
        kinds = {s.data["signature"] for s in dep_logs} | {e.data["dependency_signature"] for e in dep_exc}

        mis = Finding("dependency_misconfiguration", v.name, dependency=dep)
        una = Finding("dependency_unavailable", v.name, dependency=dep)
        if not dep_logs and not dep_exc:
            for f in (mis, una):
                f.reject(f"{v.name} logged no connection errors for {host}:{port}")
            out += [mis, una]
            continue
        total = sum(s.data["count"] for s in dep_logs)
        symptom = (f"{v.name} cannot use its {dep['type']} dependency: it logged {total} connection error(s) for "
                   f"{host}:{port} ({', '.join(sorted(k.replace('_', ' ') for k in kinds))})")
        for f in (mis, una):
            f.add(0.3, dep_logs[:3] + dep_exc[:1], "application logs show failing connections to this endpoint")
            f.say(symptom, dep_logs[:3] + dep_exc[:1])
            f.say(f"The endpoint comes from {v.name}'s configuration: {ref.data['variable']} ({ref.data['config_source']}) -> "
                  f"{host}:{port}", [ref])

        # --- misconfiguration: the configured endpoint is wrong -----------------------------
        if lookup and not lookup.data["found"]:
            mis.add(0.3, lookup, "configured host is not a Service in the cluster")
            mis.say(f"The configured host '{host}' does not exist: {lookup.text}", [lookup])
        if probe and probe.data["dns"] == "error":
            mis.add(0.15, probe, "the name does not resolve from inside the application instance")
            mis.say(f"From inside the application instance the name '{host}' does not resolve", [probe])
        if mismatch:
            mis.add(0.3, mismatch, "Service does not expose the configured port")
            mis.say(mismatch.text, [mismatch])
        if kinds & {"auth_failure", "missing_resource"} and eps and eps.data["ready"] > 0:
            mis.add(0.3, [s for s in dep_logs if s.data["signature"] in ("auth_failure", "missing_resource")][:2],
                    "the dependency is up but rejects the configured credentials/database")
            mis.say(f"{host} is reachable but rejects {v.name}'s credentials or database name", dep_logs[:2])
        if similar:
            mis.add(0.1, similar[:1], "a matching healthy service exists under a different name")
            mis.say(f"A plausible intended target exists and is healthy: {similar[0].text}", similar[:1])
        if alt_ok:
            mis.add(0.1, alt_ok[:1], "the alternative service is reachable from the same instance")
            mis.say(f"From the same instance, {alt_ok[0].data['host']}:{alt_ok[0].data['port']} accepts TCP connections, so the "
                    f"network and the database are fine", alt_ok[:1])
        if changed:
            mis.add(0.1, changed[:1] + v.changes[:1], "the configuration was changed shortly before")
            mis.say(f"{changed[0].data['config_source']} (which provides {ref.data['variable']}) was modified shortly before the "
                    f"failures started" + (", followed by a rollout of " + v.name if v.changes else ""), changed[:1] + v.changes[:1])
        if lookup and lookup.data["found"] and not mismatch and not (kinds & {"auth_failure", "missing_resource"}):
            mis.reject(f"the configured endpoint names an existing Service ({lookup.data['service']}) on a port it exposes",
                       lookup)
            mis.score *= 0.3
        mis.root_cause = (f"{v.name} is configured to reach its {dep['type']} dependency at '{host}:{port}' "
                          f"({ref.data['variable']} from {ref.data['config_source']}), " + (
                              "but no such service exists in the cluster" if lookup and not lookup.data["found"] else
                              "but that service does not expose the configured port" if mismatch else
                              "but the dependency rejects the configured credentials/database")
                          + (f"; the {dep['type']} service '{similar[0].data['service']}' is healthy and reachable"
                             if similar else ""))

        # --- unavailable: the configured endpoint is right, but nothing healthy answers ------
        if lookup and lookup.data["found"]:
            una.add(0.1, lookup, "configured endpoint names an existing Service")
            una.say(f"The configured host '{host}' is a valid Service ({lookup.text})", [lookup])
        elif lookup:
            una.score *= 0.3
            una.reject(f"'{host}' does not exist at all, so this is not an outage of an existing dependency", lookup)
        if eps and eps.data["ready"] == 0:
            una.add(0.3, eps, "the Service has no ready endpoints")
            una.say(f"Nothing is serving that Service: {eps.text}", [eps])
        elif eps:
            una.reject(f"the Service has {eps.data['ready']} ready endpoint(s)", eps)
            una.score *= 0.5
        down = [b for b in backing if b.data["ready"] == 0]
        if down:
            una.add(0.15, down, "the component behind the service has no ready replicas")
            una.say(down[0].text + (" - it has been scaled to zero" if down[0].data["desired"] == 0 else ""), down)
            what = [e for e in store.find(kind="event", subject=f"component/{down[0].data['component']}")
                    if e.data.get("category") in ("scaled", "stopped", "instance_deleted", "restart_backoff",
                                                  "health_check_failed", "scheduling_failed", "failed", "evicted")]
            if what:
                una.support.append((what[0].id, "what happened to the dependency's component"))
                una.say(f"Events for {down[0].data['component']}: {what[0].text}", what[:2])
        if probe and probe.data["dns"] == "ok" and probe.data["tcp"] in ("refused", "timeout", "error"):
            una.add(0.15, probe, "the name resolves but connections are refused/time out")
            una.say(f"From inside the application instance '{host}' resolves, but the TCP connection is "
                    f"{probe.data['tcp']}", [probe])
        elif probe and probe.data["tcp"] == "ok":
            una.reject(f"a TCP connection to {host}:{port} succeeds from the application instance", probe)
            una.score *= 0.5
        why = ("it has been scaled to zero replicas" if down and down[0].data["desired"] == 0 else
               "its component has no ready replicas" if down else "the service has no ready endpoints")
        una.root_cause = (f"{v.name}'s configuration is valid ({ref.data['variable']} -> {host}:{port}), but its "
                          f"{dep['type']} dependency is unavailable: {why}")
        una.backing = mis.backing = [b.data["component"] for b in backing]
        for f in (mis, una):
            f.score = min(f.score, 1.0)
        out += [mis, una]
    return out


def _matches(sig: Fact, host: str, port) -> bool:
    th = sig.data.get("target_host")
    if th:
        return th == host or th.split(".")[0] == host.split(".")[0]
    return sig.data.get("target_port") is not None and str(sig.data["target_port"]) == str(port)


def check_platform(v: View, store: EvidenceStore) -> list[Finding]:
    """Failures where the platform cannot run the component at all, or kills it for failing health checks."""
    out = []
    img = [w for w in v.waiting if w.data.get("cause") == "image_unavailable"]
    f = Finding("image_pull_failure", v.name)
    if img:
        f.add(0.9, img[:2], "the process image cannot be obtained")
        f.say(f"{v.name} cannot start because its image cannot be obtained: {img[0].text}", img[:2])
        f.root_cause = f"{v.name}'s image cannot be obtained ({(img[0].data.get('message') or img[0].data['reason'])[:120]})"
    else:
        f.reject("no process is waiting for an image")
    out.append(f)

    cfg = [w for w in v.waiting if w.data.get("cause") == "invalid_configuration"]
    f = Finding("container_config_error", v.name)
    if cfg:
        f.add(0.85, cfg[:2], "the platform cannot create the process from its specification")
        f.say(f"The platform cannot create {v.name}'s process: {cfg[0].text}", cfg[:2])
        f.root_cause = (f"{v.name}'s specification references configuration that cannot be resolved "
                        f"({(cfg[0].data.get('message') or '')[:120]})")
    else:
        f.reject("no process is stuck because its specification cannot be resolved")
    out.append(f)

    sched = [p for p in v.instances if p.data["unschedulable"]] + v.events_by("scheduling_failed")
    f = Finding("unschedulable", v.name)
    if sched:
        f.add(0.85, sched[:2], "instances cannot be scheduled")
        f.say(f"{v.name} instances cannot be placed on any machine", sched[:2])
        ev = next((e for e in sched if e.kind == "event"), None)
        f.root_cause = f"{v.name} instances cannot be scheduled" + (f": {ev.data['message'][:150]}" if ev else "")
    else:
        f.reject("no instance is unschedulable")
    out.append(f)

    health_kills = v.events_by("killed_by_health_check")
    unhealthy = v.events_by("health_check_failed")
    sig_kills = v.terms_by("killed")
    f = Finding("health_check_failure", v.name)
    if health_kills and sig_kills:
        f.add(0.55, health_kills[:1] + sig_kills[:2], "failed health checks made the platform restart the process")
        f.say(f"The platform restarted {v.name} after its health checks failed", health_kills[:1] + sig_kills[:2])
        f.root_cause = f"{v.name} stops answering its health check and is restarted by the platform"
    elif unhealthy:
        f.add(0.2, unhealthy[:2], "probe failures observed")
        f.reject("health-check failures are present but no process was killed because of them")
    else:
        f.reject("no health-check failures were recorded")
    out.append(f)
    return out


# --------------------------------------------------------------------------- orchestration

def diagnose(store: EvidenceStore) -> dict:
    names = [f.data["component"] for f in store.find(kind="component_status")]
    views = {n: View(store, n) for n in names}

    # Dependency graph from configuration: consumer -> backing components (or unresolved endpoints)
    edges: dict[str, set] = {n: set() for n in names}
    dep_health: dict[str, list] = {}
    for v in views.values():
        for ref in v.refs:
            subj = f"dependency/{ref.data['host']}:{ref.data['port']}"
            for b in store.find(kind="backing_component", subject=subj):
                edges[v.name].add(b.data["component"])
            probe = next((p for p in store.find(kind="connectivity_probe", subject=subj) if not p.data.get("alternative_for")), None)
            eps = next(iter(store.find(kind="service_endpoints", subject=subj)), None)
            healthy = bool(probe and probe.data["tcp"] == "ok") or bool(not probe and eps and eps.data["ready"] > 0)
            dep_health.setdefault(v.name, []).append({"endpoint": f"{ref.data['host']}:{ref.data['port']}",
                                                      "healthy": healthy, "fact": probe or eps})

    findings: list[Finding] = []
    for v in views.values():
        if not v.symptomatic() and not v.sigs:
            continue
        findings.append(check_memory(v, store))
        findings.append(check_crash(v, store, dep_health))
        findings += check_dependencies(v, store)
        findings += check_platform(v, store)

    # Follow the chain: a consumer's dependency symptoms are explained when the component it calls has its own,
    # better-supported failure (a crash, OOM, or a broken dependency of its own further down).
    best: dict[str, Finding] = {}
    for f in sorted(findings, key=lambda f: -f.score):
        if f.score >= 0.4:
            best.setdefault(f.component, f)
    for f in findings:
        root = next((best[b] for b in f.backing if b in best and best[b].score >= f.score * 0.8), None)
        if f.category in ("dependency_unavailable", "dependency_misconfiguration") and root and f.score > 0:
            if f.component not in root.impacted:
                root.impacted.append(f.component)
                root.say(f"{f.component} depends on {root.component} and sees connection failures to it "
                         f"(an effect of this failure, not a separate cause)", [s[0] for s in f.support[:2]])
            f.reject(f"explained by {root.component}'s own failure ({CATEGORY_LABELS[root.category]})")
            f.score *= 0.3
            f.explained_by = root.component

    ranked = sorted(findings, key=lambda f: (-round(f.score, 3), f.category == "health_check_failure"))
    primary = ranked[0] if ranked and ranked[0].score >= 0.3 else None
    symptoms = [f for f in store.find(kind="detection_signal")] + \
               [f for f in store.find(kind="entry_probe") if (f.data.get("status") or 0) >= 500 or f.data.get("status") == 0]
    if primary is None:
        affected = [n for n, v in views.items() if v.symptomatic()]
        return {
            "category": "undetermined", "category_label": CATEGORY_LABELS["undetermined"],
            "affected_component": affected[0] if affected else None, "dependencies": [], "impacted_components": [],
            "symptoms": [f.id for f in symptoms] + [x.id for n in affected for x in views[n].symptom_facts()],
            "reasoning": [{"statement": "The collected evidence does not support any known failure category strongly "
                                        "enough to name a root cause", "facts": []}],
            "root_cause": "Undetermined - see observed symptoms and evidence",
            "confidence": 0.0, "confidence_label": "Low", "evidence": [], "contributing_factors": [],
            "alternatives": _alternatives(ranked, None), "component_summary": _component_summary(views),
        }

    # Consumers (direct or transitive) of the affected component that show symptoms are impacted.
    impacted = set(primary.impacted)
    frontier = {primary.component}
    while frontier:
        nxt = {c for c, deps in edges.items() if deps & frontier and c not in impacted and c != primary.component}
        nxt = {c for c in nxt if views[c].symptomatic() or views[c].sigs}
        impacted |= nxt
        frontier = nxt
    for c in sorted(impacted):
        up = [s for s in views[c].sigs if s.data["signature"] in DEPENDENCY_SIGNATURES | {"upstream_failure"}]
        primary.say(f"{c} (which calls {primary.component}) is impacted: " + (up[0].text if up else "it logged errors"),
                    up[:1] or ([views[c].levels] if views[c].levels else []))
    for f in store.find(kind="entry_probe"):
        if (f.data.get("status") or 0) >= 500 or f.data.get("status") == 0:
            primary.say(f"Users are affected: {f.text}", [f])

    runner_up = next((f for f in ranked[1:] if (f.component, f.category) != (primary.component, primary.category)
                      and f.score >= 0.3 and not f.explained_by), None)  # weak/rejected findings don't compete
    confidence = max(0.05, min(0.97, primary.score * (1 - 0.5 * (runner_up.score if runner_up else 0))))
    missing = [s for s in store.trace if s.get("step") in ("capability", "tool")  # "tool": pre-Iteration-3 evidence
               and str(s.get("status", "")).startswith("error")]
    if missing:
        confidence *= 0.9
    dep = primary.dependency
    if dep:
        eps = next(iter(store.find(kind="service_endpoints", subject=f"dependency/{dep['endpoint']}")), None)
        dep = {**dep, "state": eps.text if eps else "no such service in the cluster"}
    evidence = list(dict.fromkeys([s[0] for s in primary.support] + [fid for r in primary.reasoning for fid in r["facts"]]))
    return {
        "category": primary.category,
        "category_label": CATEGORY_LABELS[primary.category],
        "affected_component": primary.component,
        "dependencies": [dep] if dep else [],
        "impacted_components": sorted(impacted),
        "symptoms": list(dict.fromkeys([f.id for f in symptoms] + [x.id for x in views[primary.component].symptom_facts()]
                                       + [x.id for c in impacted for x in views[c].symptom_facts()[:2]])),
        "reasoning": primary.reasoning,
        "root_cause": primary.root_cause,
        "contributing_factors": primary.contributing,
        "confidence": round(confidence, 2),
        "confidence_label": "High" if confidence >= 0.75 else "Medium" if confidence >= 0.5 else "Low",
        "score": round(primary.score, 2),
        "evidence": evidence,
        "alternatives": _alternatives(ranked, primary),
        "component_summary": _component_summary(views),
    }


def _alternatives(ranked: list[Finding], primary: Finding | None) -> list[dict]:
    out, seen = [], set()
    focus = primary.component if primary else None
    for f in ranked:
        if f is primary or (f.category, f.component) in seen:
            continue
        if focus and f.component != focus and f.score < 0.2:
            continue
        seen.add((f.category, f.component))
        reasons = [r for _, r in f.against] or ["supporting evidence is weaker than for the diagnosis"]
        out.append({"category": f.category, "label": CATEGORY_LABELS[f.category], "component": f.component,
                    "score": round(f.score, 2), "why_not": "; ".join(dict.fromkeys(reasons)),
                    "facts": [fid for fid, _ in f.against if fid]})
    return out[:10]


def _component_summary(views: dict) -> list[dict]:
    return [{"component": n, "status": v.status.text if v.status else "", "symptomatic": v.symptomatic(),
             "terminations": len(v.terms), "error_lines": v.error_lines} for n, v in views.items()]
