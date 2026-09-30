"""Investigation planner (Iteration 3): decides which evidence is relevant and what to examine next.

    Incident context -> examine suspects -> evaluate evidence -> decide next step -> ... -> stop

The planner is deterministic and evidence-driven. It starts from the components named by the
detection signals and follows the evidence:

  * a process restarted             -> read its previous instance's logs (why did it end?)
  * errors about a dependency, or the dependency looks unhealthy
                                    -> test connectivity from inside the consumer, and examine the
                                       component that serves the dependency
  * a suspect shows no failure of its own
                                    -> follow its dependencies downstream (the cause is further down)
  * signs of resource exhaustion    -> query metrics (traffic, memory) if a metrics provider exists
  * nothing failing found at all    -> widen to every component (survey)

Every decision is written to the investigation trace with its reason, so a report shows not just what
was found but why each piece of evidence was gathered. It only ever asks the capability layer for
evidence and never changes the system. The planner could later be assisted by an LLM (a later
milestone), but facts would still come only from capabilities.
"""
from collections import deque

from . import collect as rec
from .capabilities import Capabilities
from .context import IncidentContext
from .dependencies import check_dependency, probe_dependency
from .evidence import EvidenceStore
from .logparse import DEPENDENCY_SIGNATURES

FOLLOW_SIGNATURES = DEPENDENCY_SIGNATURES | {"upstream_failure"}
MAX_COMPONENTS = 25  # safety budget for very large scopes


class InvestigationPlanner:
    def __init__(self, caps: Capabilities, ctx: IncidentContext, log=print):
        self.caps, self.ctx, self.log = caps, ctx, log
        self.store: EvidenceStore = caps.store
        self.queue: deque = deque()
        self.queued: set = set()
        self.examined: list[str] = []
        self.states: dict = {}

    # -- decisions --------------------------------------------------------------------
    def decide(self, action: str, target: str, reason: str) -> None:
        self.store.step("decision", f"{action} {target}".strip(), reason=reason)
        self.log(f"  decide: {action} {target} - {reason}")

    def enqueue(self, component: str, reason: str) -> None:
        if component in self.components and component not in self.queued and len(self.queued) < MAX_COMPONENTS:
            self.queued.add(component)
            self.queue.append(component)
            self.decide("examine", component, reason)

    # -- run ------------------------------------------------------------------------------
    def run(self) -> EvidenceStore:
        caps, ctx, store = self.caps, self.ctx, self.store
        tr = ctx.window
        rec.record_signals(store, ctx.incident)
        self.components = caps.list_components() or []
        self.decide("collect", "events and deployment history",
                    "cheap, window-wide context: what happened and what changed recently")
        rec.record_events(store, caps, tr)
        rec.record_changes(store, caps, tr)
        rec.record_services(store, caps)
        if ctx.entry:
            self.decide("probe", ctx.entry[0], "confirm the user-facing symptom with a synthetic request")
            rec.record_entry_probe(store, caps, ctx.entry)

        suspects = [s for s in ctx.suspects if s in self.components]
        if suspects:
            for s in suspects:
                self.enqueue(s, "named by a detection signal")
        else:
            for c in self.components:
                self.enqueue(c, "no signal names a component: survey everything")

        while self.queue:
            self.examine(self.queue.popleft())

        if not any(self.failing(c) for c in self.examined):
            rest = [c for c in self.components if c not in self.queued]
            for c in rest:
                self.enqueue(c, "no failing component found yet: widening the search")
            while self.queue:
                self.examine(self.queue.popleft())

        self.maybe_metrics()
        store.step("investigation_complete", f"examined {len(self.examined)} of {len(self.components)} components",
                   examined=self.examined, skipped=[c for c in self.components if c not in self.examined])
        return store

    def examine(self, c: str) -> None:
        caps, store, tr = self.caps, self.store, self.ctx.window
        rs = caps.get_resource_state(c, tr)
        if rs is None:
            return
        self.examined.append(c)
        self.states[c] = rs
        rec.record_resource_state(store, caps, rs, tr.start)
        restarted = [f"{i.name}/{p.name}" for i in rs.instances for p in i.processes if p.restarts > 0]
        if restarted:
            self.decide("read previous logs of", c, f"{len(restarted)} process(es) restarted; the ended instance's "
                        f"last output may show why ({', '.join(restarted[:3])})")
        rec.record_logs(store, caps, rs, tr, include_previous=bool(restarted))

        own_failure = self.failing(c)
        for ref in caps.get_dependencies(c) or []:
            summary = check_dependency(caps, store, c, ref, tr.start, probe=False)
            errors = self.errors_about(c, ref.host, ref.port)
            unhealthy = summary["exists"] is False or summary["ready"] == 0 or any(
                f.data["ready"] < f.data["desired"] for f in store.find(kind="backing_workload", subject=summary["subject"]))
            if ref.port and (errors or unhealthy):
                why = (f"{c} logged {sum(f.data.get('count', 1) for f in errors)} error(s) about {ref.host}:{ref.port}"
                       if errors else f"{ref.host}:{ref.port} looks unhealthy")
                self.decide("test connectivity", f"{c} -> {ref.host}:{ref.port}", why)
                probe_dependency(caps, store, c, ref)
            for b in summary["backing"]:
                if errors:
                    self.enqueue(b, f"{c}'s errors point at {ref.host}, served by {b}")
                elif unhealthy:
                    self.enqueue(b, f"{ref.host} (served by {b}) looks unhealthy")
                elif not own_failure and c in self.ctx.suspects:
                    self.enqueue(b, f"{c} shows no failure of its own; the cause may be downstream in {b}")

    def maybe_metrics(self) -> None:
        exhaustion = [f for f in self.store.facts if (f.kind == "container_terminated" and
                                                      (f.data.get("reason") == "OOMKilled" or f.data.get("exit_code") == 137))
                      or (f.kind == "log_signature" and f.data.get("signature") == "memory_pressure")]
        if not exhaustion:
            self.decide("skip", "metrics", "no sign of resource exhaustion")
            return
        if self.caps.metrics is None:
            self.decide("skip", "metrics", "resource exhaustion suspected but no metrics provider is configured")
            return
        self.decide("query", "metrics (traffic, errors, memory)",
                    f"resource exhaustion suspected ({exhaustion[0].text[:90]})")
        from .metrics import metric_facts
        try:
            metric_facts(self.store, self.caps, self.ctx.incident, self.ctx.window, list(self.states.values()),
                         self.ctx.metrics_target)
        except Exception as exc:  # noqa: BLE001 - metrics are optional
            self.store.step("metrics", f"skipped: {exc}")

    # -- evidence evaluation -------------------------------------------------------------
    def failing(self, c: str) -> bool:
        """Does component c show a failure of its own (not just a symptom seen elsewhere)?"""
        subj = f"workload/{c}"
        for f in self.store.facts:
            if f.subject != subj:
                continue
            if f.kind == "container_terminated" or (f.kind == "container_waiting" and f.data.get("problematic")):
                return True
            if f.kind == "pod_status" and not f.data.get("ready"):
                return True
            if f.kind == "log_levels" and f.data.get("errors", 0) > 0:
                return True
        return False

    def errors_about(self, c: str, host: str, port) -> list:
        """Log evidence in c that refers to the dependency host:port."""
        short = host.split(".")[0]
        out = []
        for f in self.store.find(kind="log_signature", subject=f"workload/{c}"):
            if f.data["signature"] not in FOLLOW_SIGNATURES:
                continue
            th = f.data.get("target_host")
            if (th and (th == host or th.split(".")[0] == short)) or (
                    not th and f.data.get("target_port") and str(f.data["target_port"]) == str(port)):
                out.append(f)
        for f in self.store.find(kind="log_exception", subject=f"workload/{c}"):
            msg = f.data.get("message", "")
            if f.data.get("dependency_signature") and (host in msg or f"port {port}" in msg):
                out.append(f)
        return out


def plan_and_collect(caps: Capabilities, ctx: IncidentContext, log=print) -> EvidenceStore:
    return InvestigationPlanner(caps, ctx, log).run()
