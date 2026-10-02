"""Kubernetes adapter: implements the capability interface on top of the Kubernetes API.

This is the only place in the investigation path that knows about Deployments, Pods, ReplicaSets,
Services/Endpoints, ConfigMaps/Secrets, events, the pod journal and `kubectl exec`. The engine
(collect.py, dependencies.py, diagnosis.py) sees only the records defined in base.py.

Evidence history (Iteration 4): when a HistoryStore is attached, the adapter also answers from what the
KubernetesRecorder retained (runs and pods that no longer exist, expired events, earlier configuration), and
marks such evidence as retained. It never presents retained evidence as current state.
"""
import difflib
import json
from pathlib import Path

from ..kube import read_journal
from .base import (AvailabilityChange, BackingComponent, Change, ConfigChange, ConfigEntry, ConnectivityResult, Coverage,
                   DependencyRef,
                   EventRecord, InstanceState, LogHistory, PastInstance, ProcessState, RequestResult, ResourceProvider,
                   ResourceState, ServiceHealth, SimilarService, Termination, TerminationRecord, TimeRange)
from .references import PORT_TYPES, extract_references

HISTORY_SOURCE = "kubernetes.history"


# Kubernetes waiting reasons -> neutral causes (capabilities/base.py ProcessState.waiting_cause)
WAITING_CAUSES = {
    "CrashLoopBackOff": "restart_backoff",
    "ImagePullBackOff": "image_unavailable", "ErrImagePull": "image_unavailable", "InvalidImageName": "image_unavailable",
    "CreateContainerConfigError": "invalid_configuration", "CreateContainerError": "invalid_configuration",
    "RunContainerError": "invalid_configuration",
}


EVENT_CATEGORIES = {
    "BackOff": "restart_backoff", "Unhealthy": "health_check_failed", "FailedScheduling": "scheduling_failed",
    "ScalingReplicaSet": "scaled", "SuccessfulDelete": "instance_deleted", "Evicted": "evicted",
    "Preempted": "evicted", "OOMKilling": "memory_limit", "Failed": "failed",
    "NodeNotReady": "node_problem", "SystemOOM": "node_problem", "EvictionThresholdMet": "node_problem",
    "NodeHasInsufficientMemory": "node_problem", "Rebooted": "node_problem",
}


def termination_cause(reason: str | None, exit_code: int | None) -> str | None:
    """Kubernetes termination -> neutral cause (capabilities/base.py Termination.cause)."""
    if reason == "OOMKilled":
        return "memory_limit"
    if exit_code in (137, 143):          # SIGKILL / SIGTERM from outside the process
        return "killed"
    if exit_code == 0 or reason == "Completed":
        return "completed"
    if exit_code is not None or reason == "Error":
        return "error_exit"
    return None


def event_category(reason: str | None, message: str) -> str | None:
    """Kubernetes event reason -> neutral category (capabilities/base.py EventRecord.category)."""
    if reason == "Killing":
        return "killed_by_health_check" if "liveness" in (message or "").lower() else "stopped"
    return EVENT_CATEGORIES.get(reason)


class KubernetesAdapter(ResourceProvider):
    name = "kubernetes"

    def __init__(self, kube, namespace: str, journal_path: Path | None = None, active_probes: bool = True,
                 clock=None, history=None, recorder_options: dict | None = None):
        self.kube, self.ns, self.journal_path, self.active_probes = kube, namespace, journal_path, active_probes
        self.scope = namespace
        self.clock = clock
        self.history = history                      # HistoryStore, or None (no evidence history)
        self.recorder_options = recorder_options or {}
        self._cache: dict = {}
        self._journal = None
        self._recorder = None

    def reset(self) -> None:
        self._cache.clear()

    def start_background_recording(self) -> None:
        """Kubernetes forgets earlier runs, deleted pods, old events and earlier configuration. With a history
        store the recorder retains them; without one, the older pod journal still records terminations."""
        import time
        if self.history is not None:
            if self._recorder is None:
                from .kubernetes_recorder import KubernetesRecorder
                self._recorder = KubernetesRecorder(self.kube, self.ns, self.history, clock=self.clock or time.time,
                                                    **self.recorder_options)
                self._recorder.start()
            return
        if self.journal_path is None or self._journal is not None:
            return
        from ..kube import PodJournal
        self._journal = PodJournal(self.kube, self.ns, self.journal_path, clock=self.clock or time.time)
        self._journal.start()

    def stop_background_recording(self) -> None:
        if self._recorder is not None:
            self._recorder.stop()

    # -- raw reads (cached per investigation) -----------------------------------------
    def _once(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def _workloads(self) -> list[dict]:
        return self._once("workloads", lambda: self.kube.workloads(self.ns))

    def _pods(self) -> list[dict]:
        return self._once("pods", lambda: self.kube.pods(self.ns))

    def _replicasets(self) -> list[dict]:
        return self._once("replicasets", lambda: self.kube.replicasets(self.ns))

    def _workload(self, name: str) -> dict | None:
        return next((w for w in self._workloads() if w["name"] == name), None)

    def _pods_of(self, w: dict) -> list[dict]:
        sel = w.get("selector") or {}
        return [p for p in self._pods() if sel and all(p["labels"].get(k) == v for k, v in sel.items())]

    # -- capabilities -------------------------------------------------------------
    def list_components(self) -> list[str]:
        return [w["name"] for w in self._workloads()]

    def get_resource_state(self, component: str, time_range: TimeRange) -> ResourceState | None:
        w = self._workload(component)
        if w is None:
            return None
        pods = self._pods_of(w)
        names = {p["name"] for p in pods}
        app_label = (w.get("selector") or {}).get("app")
        history = []
        for e in read_journal(self.journal_path, time_range.start - 60, time_range.end + 60) if self.journal_path else []:
            if e["kind"] != "container_terminated" or not e.get("app"):
                continue
            # Pods that still exist, and pods of this workload that have since been deleted (e.g. replaced by a
            # rollout): their terminations are still evidence, even though their logs are gone.
            gone = e["pod"] not in names
            if gone and not (e["app"] == app_label or e["pod"].startswith(w["name"] + "-")):
                continue
            history.append(TerminationRecord(
                instance=e["pod"], process=e.get("container"), restarts=e.get("restart_count"),
                termination=Termination(e.get("reason"), e.get("exit_code"), e.get("started_at"), e["t"],
                                        termination_cause(e.get("reason"), e.get("exit_code"))),
                source="kubernetes.pod_journal", instance_gone=gone))
        past = []
        if self.history is not None:
            from ..kube import parse_mem
            mem_limits = {c["name"]: parse_mem((c["limits"] or {}).get("memory")) for c in w["containers"]}
            runs = self._retained_runs(w["name"], time_range)
            for e in self.history.lifecycle(self.ns, w["name"], time_range.start - 60, time_range.end + 60, "terminated"):
                d = e["data"]
                gen = d.get("generation")
                history.append(TerminationRecord(
                    instance=e["instance"], process=e["process"], restarts=d.get("restart_count"),
                    termination=Termination(d.get("reason"), d.get("exit_code"), d.get("started_at"), e["t"],
                                            termination_cause(d.get("reason"), d.get("exit_code"))),
                    source=HISTORY_SOURCE, instance_gone=e["instance"] not in names,
                    logs_retained=(e["instance"], e["process"], gen) in runs, generation=gen,
                    memory_limit_bytes=mem_limits.get(e["process"]), observed_at=e["observed_at"]))
            for i in self.history.instances(self.ns, w["name"], time_range.start, time_range.end):
                if i["instance"] not in names:
                    past.append(PastInstance(i["instance"], i["kind"] or "Pod", i["created"], i["gone_at"],
                                             sum(1 for r in runs if r[0] == i["instance"])))
        return ResourceState(
            component=w["name"], kind=w["kind"], scope=w["namespace"], desired=w["replicas_desired"],
            ready=w["replicas_ready"], available=w["replicas_available"],
            limits={c["name"]: c["limits"] for c in w["containers"]},
            instances=[_instance(p) for p in pods], history=history, past_instances=past,
            images={c["name"]: c.get("image") for c in w["containers"]})

    def _retained_runs(self, component: str, time_range: TimeRange) -> set[tuple]:
        return {(g["instance"], g["process"], g["generation"])
                for g in self.history.log_generations(self.ns, component, time_range.start - 600, time_range.end + 60)}

    def get_events(self, time_range: TimeRange, infrastructure: bool = False) -> list[EventRecord]:
        if infrastructure:
            return [EventRecord(None, "Node", e["object_name"], e["type"], e["reason"], e["message"], e.get("count") or 1,
                                e["first"], e["last"], event_category(e["reason"], e["message"]))
                    for e in self.kube.node_events() if (e["last"] or e["first"] or 0) >= time_range.start]
        rs_owner = {rs["name"]: rs["deployment"] for rs in self._replicasets()}
        pod_workload = {p["name"]: w["name"] for w in self._workloads() for p in self._pods_of(w)}
        out = []
        for e in self.kube.events(self.ns):
            if (e["last"] or e["first"] or 0) < time_range.start:
                continue
            obj = e["object_name"]
            comp = pod_workload.get(obj) or rs_owner.get(obj) or (obj if e["object_kind"] in ("Deployment", "StatefulSet") else None)
            if comp is None:  # pods that no longer exist: attribute by ReplicaSet/StatefulSet name prefix
                comp = next((w for w in set(rs_owner.values()) | set(pod_workload.values()) if w and obj.startswith(w + "-")), obj)
            out.append(EventRecord(comp, e["object_kind"], obj, e["type"], e["reason"], e["message"], e["count"],
                                   e["first"], e["last"], event_category(e["reason"], e["message"])))
        if self.history is not None:
            # Events the platform has since expired, or that were recorded before the pod disappeared.
            live = {(e.object_name, e.reason, round(e.first or 0)) for e in out}
            for e in self.history.events(self.ns, time_range.start, time_range.end):
                if (e["last"] or e["first"] or 0) < time_range.start or \
                        (e["object_name"], e["reason"], round(e["first"] or 0)) in live:
                    continue
                comp = e["component"] or next((w for w in set(rs_owner.values()) | set(pod_workload.values())
                                               if w and e["object_name"].startswith(w + "-")), e["object_name"])
                out.append(EventRecord(comp, e["object_kind"], e["object_name"], e["type"], e["reason"], e["message"],
                                       e["count"], e["first"], e["last"], event_category(e["reason"], e["message"]),
                                       origin="retained"))
        return out

    def get_logs(self, component: str, instance: str, process: str, time_range: TimeRange,
                 previous: bool = False) -> list[tuple[float | None, str]]:
        if previous:
            return self.kube.logs(self.ns, instance, process, previous=True)
        return self.kube.logs(self.ns, instance, process, since_s=int(max(1, time_range.end - time_range.start)) + 30)

    def get_configuration(self, component: str) -> list[ConfigEntry]:
        w = self._workload(component)
        if w is None:
            return []
        cms = {c["name"]: c for c in self._once("configmaps", lambda: self.kube.configmaps(self.ns))}
        secret = lambda name: self._once(("secret", name), lambda: self.kube.secret_values(self.ns, name))  # noqa: E731
        out = []
        for c in w["containers"]:
            for ef in c["env_from"]:
                if "configmap" in ef:
                    cm = cms.get(ef["configmap"], {})
                    for k, v in cm.get("data", {}).items():
                        out.append(ConfigEntry(c["name"], k, v, False, f"configmap/{ef['configmap']}", cm.get("last_modified")))
                else:
                    for k, v in (secret(ef["secret"]) or {}).items():
                        out.append(ConfigEntry(c["name"], k, v, True, f"secret/{ef['secret']}", None))
            for e in c["env"]:
                src, val, sensitive, modified = "literal", e["value"], False, None
                if e["from"] and "configmap" in e["from"]:
                    cm = cms.get(e["from"]["configmap"], {})
                    val, src, modified = cm.get("data", {}).get(e["from"]["key"]), f"configmap/{e['from']['configmap']}", cm.get("last_modified")
                elif e["from"] and "secret" in e["from"]:
                    val = (secret(e["from"]["secret"]) or {}).get(e["from"]["key"])
                    src, sensitive = f"secret/{e['from']['secret']}", True
                elif e["from"] and "field" in e["from"]:
                    src, val = f"field/{e['from']['field']}", None
                out.append(ConfigEntry(c["name"], e["name"], val, sensitive, src, modified))
        return out

    def get_dependencies(self, component: str) -> list[DependencyRef]:
        return extract_references(self.get_configuration(component))

    def list_services(self) -> list[ServiceHealth]:
        out = []
        for s in self.kube.services(self.ns):
            ep = self.kube.endpoints(self.ns, s["name"]) or {"ready": [], "not_ready": []}
            out.append(self._health(s["name"], None, s, ep))
        return out

    def get_service_health(self, host: str, port: int | None, dep_type: str) -> ServiceHealth:
        name, ns, internal = _split_host(host, self.ns)
        if not internal:
            return ServiceHealth(host=host, port=port, internal=False, exists=None, name=host, scope=None)
        services = self._once("all_services", lambda: self.kube.services(None))
        svc = next((s for s in services if s["name"] == name and s["namespace"] == ns), None)
        if svc is None:
            h = ServiceHealth(host=host, port=port, internal=True, exists=False, name=name, scope=ns,
                              kind="Service", scope_kind="namespace", address_kind="ClusterIP", instance_kind="pod",
                              other_scopes=[s["namespace"] for s in services if s["name"] == name])
            for s in services:  # what the configuration might have meant: same port/type, or a similar name
                if s["namespace"] != ns:
                    continue
                ports = [p["port"] for p in s["ports"]]
                sim = difflib.SequenceMatcher(None, s["name"], name).ratio()
                if port in ports or any(PORT_TYPES.get(p) == dep_type for p in ports) or sim >= 0.6:
                    ep = self.kube.endpoints(s["namespace"], s["name"]) or {"ready": []}
                    h.similar.append(SimilarService(s["name"], ports, len(ep["ready"]), round(sim, 2), port in ports))
            return h
        ep = self.kube.endpoints(svc["namespace"], svc["name"]) or {"ready": [], "not_ready": []}
        h = self._health(host, port, svc, ep)
        if svc["namespace"] == self.ns and svc["selector"]:
            for w in self._workloads():
                if w["selector"] and all(w["selector"].get(k) == v for k, v in svc["selector"].items()):
                    h.backing.append(BackingComponent(w["name"], w["kind"], w["replicas_desired"], w["replicas_ready"]))
        return h

    def _health(self, host, port, svc, ep) -> ServiceHealth:
        return ServiceHealth(
            host=host, port=port, internal=True, exists=True, name=svc["name"], scope=svc["namespace"],
            kind="Service", scope_kind="namespace", address_kind="ClusterIP", instance_kind="pod",
            selector=svc["selector"], address=svc["cluster_ip"], ports=[p["port"] for p in svc["ports"]],
            ready_endpoints=len(ep["ready"]), not_ready_endpoints=len(ep["not_ready"]),
            endpoint_instances=[a["pod"] or a["ip"] for a in ep["ready"]])

    def check_connectivity(self, from_component: str, host: str, port: int) -> ConnectivityResult:
        """Fixed, read-only DNS + TCP check from inside a running instance of `from_component`."""
        w = self._workload(from_component)
        runner = next((p for p in (self._pods_of(w) if w else []) if p["phase"] == "Running"
                       and any((c["state"] or {}).get("state") == "running" for c in p["containers"])), None)
        if runner is None:
            return ConnectivityResult(None, host, port, skipped="no running instance")
        if not self.active_probes:
            return ConnectivityResult(runner["name"], host, port, skipped="active probes disabled")
        ctr = next(c["name"] for c in runner["containers"] if (c["state"] or {}).get("state") == "running")
        r = self.kube.probe_tcp(self.ns, runner["name"], ctr, host, int(port)) or {}
        if r.get("skipped") or r.get("error"):
            return ConnectivityResult(runner["name"], host, port, skipped=r.get("skipped") or r.get("error"))
        if r.get("dns") != "ok":
            return ConnectivityResult(runner["name"], host, port, dns="error", error=r.get("dns_error"))
        return ConnectivityResult(runner["name"], host, port, dns="ok", tcp=r.get("tcp"), addresses=r.get("addresses", []),
                                  error=r.get("tcp_error"), ms=r.get("tcp_ms"))

    def probe_request(self, service: str, port: str, path: str) -> RequestResult:
        status, body = self.kube.service_proxy_get(self.ns, service, port, path)
        return RequestResult(status, body)

    def get_deployment_history(self, time_range: TimeRange) -> list[Change]:
        """Rollouts (new ReplicaSet revisions after the first) inside the range."""
        kinds = {w["name"]: w["kind"] for w in self._workloads()}
        return [Change(rs["deployment"], kinds.get(rs["deployment"], "Deployment"), "rollout", rs["created"],
                       rs["revision"], f"ReplicaSet {rs['name']}")
                for rs in self._replicasets()
                if rs["created"] and time_range.start <= rs["created"] <= time_range.end and rs["revision"] not in (None, "1")]

    # -- evidence history (Iteration 4) -------------------------------------------------
    def get_log_history(self, component: str, time_range: TimeRange) -> list[LogHistory]:
        """Retained runs the live API can no longer serve: every run of a pod that is gone, and runs older than
        the previous one of a pod that still exists (Kubernetes keeps only the current and previous run)."""
        if self.history is None:
            return []
        live = {p["name"]: {c["name"]: c["restart_count"] for c in p["containers"]} for p in self._pods()}
        ends = {(e["instance"], e["process"], e["data"].get("generation")): e
                for e in self.history.lifecycle(self.ns, component, time_range.start - 600, time_range.end + 60, "terminated")}
        out = []
        for g in self.history.log_generations(self.ns, component, time_range.start, time_range.end):
            inst, proc, gen = g["instance"], g["process"], g["generation"]
            if inst in live and proc in live[inst] and gen >= live[inst][proc] - 1:
                continue    # still readable live (current or previous run): never double-count
            end = ends.get((inst, proc, gen))
            d = end["data"] if end else {}
            out.append(LogHistory(
                instance=inst, process=proc, generation=gen, instance_gone=inst not in live,
                lines=self.history.log_lines(self.ns, inst, proc, gen, time_range.start, time_range.end),
                dropped=sum(x["dropped"] for x in self.history.log_gaps(self.ns, inst, proc, gen, time_range.start,
                                                                         time_range.end)),
                termination=Termination(d.get("reason"), d.get("exit_code"), d.get("started_at"), end["t"],
                                        termination_cause(d.get("reason"), d.get("exit_code"))) if end else None))
        return out

    def get_configuration_history(self, component: str, time_range: TimeRange) -> list[ConfigChange]:
        """Changes the recorder observed in the ConfigMaps and Secrets a workload uses, and in its definition."""
        if self.history is None:
            return []
        w = self._workload(component)
        sources = [(w["kind"], w["name"])] if w else []
        for c in (w or {}).get("containers", []):
            sources += [("ConfigMap", ef["configmap"]) if "configmap" in ef else ("Secret", ef["secret"])
                        for ef in c["env_from"]]
            sources += [("ConfigMap", e["from"]["configmap"]) if "configmap" in e["from"] else ("Secret", e["from"]["secret"])
                        for e in c["env"] if e["from"] and ("configmap" in e["from"] or "secret" in e["from"])]
        out = []
        for kind, name in dict.fromkeys(sources):
            versions = self.history.versions(self.ns, kind, name, time_range.start, time_range.end)
            for old, new in zip(versions, versions[1:]):
                a, b = _flatten(old["content"]), _flatten(new["content"])
                exact = kind == "ConfigMap" and new["modified_at"] is not None
                for item in sorted(set(a) | set(b)):
                    if a.get(item) == b.get(item):
                        continue
                    sensitive = kind == "Secret"
                    out.append(ConfigChange(
                        component=component, source=f"{kind.lower() if kind in ('ConfigMap', 'Secret') else kind}/{name}",
                        item=item, before=None if sensitive else a.get(item), after=None if sensitive else b.get(item),
                        sensitive=sensitive, t=new["modified_at"] if exact else None,
                        t_earliest=None if exact else new["previous_checked_at"], t_latest=None if exact else new["observed_at"],
                        observed_at=new["observed_at"]))
        return out

    def get_availability_history(self, host: str, port: int | None, time_range: TimeRange) -> list[AvailabilityChange]:
        """Recorded ready endpoints of the Service a host names, and ready replicas of the workloads behind it."""
        if self.history is None:
            return []
        from .kubernetes_recorder import ENDPOINTS_KIND, STATUS_KIND
        name, ns, internal = _split_host(host, self.ns)
        if not internal or ns != self.ns:
            return []                               # the recorder covers its own namespace only
        svc = next((s for s in self._once("all_services", lambda: self.kube.services(None))
                    if s["name"] == name and s["namespace"] == ns), None)
        backing = [w["name"] for w in self._workloads() if svc and svc["selector"] and w["selector"]
                   and all(w["selector"].get(k) == v for k, v in svc["selector"].items())]
        out = []
        for kind, obj in [(ENDPOINTS_KIND, name)] + [(STATUS_KIND, b) for b in backing]:
            for v in self.history.versions(self.ns, kind, obj, time_range.start, time_range.end):
                c = v["content"]
                if kind == ENDPOINTS_KIND:
                    out.append(AvailabilityChange(obj, "service", c["ready"], c["ready"] + c["not_ready"],
                                                  v["previous_checked_at"], v["observed_at"], v["observed_at"]))
                else:
                    out.append(AvailabilityChange(obj, "component", c["ready"], c["desired"],
                                                  v["previous_checked_at"], v["observed_at"], v["observed_at"]))
        return out

    def get_evidence_coverage(self, time_range: TimeRange) -> list[Coverage]:
        if self.history is None:
            return []
        return [Coverage(HISTORY_SOURCE, s["started_at"], s["last_seen_at"],
                         "pod lifecycle, log lines of every run, events, configuration and workload definitions")
                for s in self.history.sessions(self.ns, time_range.start, time_range.end)]


def _instance(p: dict) -> InstanceState:
    procs = []
    for c in p["containers"]:
        st, ls = c["state"] or {}, c["last_state"] or {}
        last = None
        if ls.get("state") == "terminated":
            last = Termination(ls.get("reason"), ls.get("exit_code"), ls.get("started_at"), ls.get("finished_at"),
                               termination_cause(ls.get("reason"), ls.get("exit_code")))
        procs.append(ProcessState(
            name=c["name"], state=st.get("state"), started_at=st.get("started_at"),
            waiting_reason=st.get("reason") if st.get("state") == "waiting" else None,
            waiting_cause=WAITING_CAUSES.get(st.get("reason")) if st.get("state") == "waiting" else None,
            waiting_message=st.get("message") if st.get("state") == "waiting" else None,
            restarts=c["restart_count"], last_termination=last,
            memory_limit_bytes=c["memory_limit_bytes"], cpu_limit_cores=c["cpu_limit_cores"]))
    return InstanceState(name=p["name"], kind="Pod", phase=p["phase"], ready=p["ready"], ready_since=p["ready_since"],
                         unschedulable=p["unschedulable"], created=p["created"], processes=procs,
                         process_kind="container")


def _flatten(content: dict, prefix: str = "") -> dict[str, str]:
    """Nested definition -> {"containers[backend].image": "...", ...} so versions can be compared item by item."""
    out = {}
    for k, v in content.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        elif isinstance(v, list) and all(isinstance(x, dict) and "name" in x for x in v):
            for x in v:
                out.update(_flatten({n: m for n, m in x.items() if n != "name"}, f"{key}[{x['name']}]"))
        else:
            out[key] = v if isinstance(v, str) or v is None else json.dumps(v, sort_keys=True)
    return out


def _split_host(host: str, default_ns: str) -> tuple[str, str | None, bool]:
    """Map a hostname to (Service name, namespace, resolvable inside the cluster)."""
    parts = host.split(".")
    if len(parts) == 1:
        return parts[0], default_ns, True
    if len(parts) >= 3 and parts[2] == "svc":
        return parts[0], parts[1], True
    if len(parts) == 2:
        return parts[0], parts[1], True
    return host, None, False
