"""Kubernetes adapter: implements the capability interface on top of the Kubernetes API.

This is the only place in the investigation path that knows about Deployments, Pods, ReplicaSets,
Services/Endpoints, ConfigMaps/Secrets, events, the pod journal and `kubectl exec`. The engine
(collect.py, dependencies.py, diagnosis.py) sees only the records defined in base.py.
"""
import difflib
from pathlib import Path

from ..kube import read_journal
from .base import (BackingComponent, Change, ConfigEntry, ConnectivityResult, DependencyRef, EventRecord,
                   InstanceState, ProcessState, RequestResult, ResourceProvider, ResourceState, ServiceHealth,
                   SimilarService, Termination, TerminationRecord, TimeRange)
from .references import PORT_TYPES, extract_references


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
                 clock=None):
        self.kube, self.ns, self.journal_path, self.active_probes = kube, namespace, journal_path, active_probes
        self.scope = namespace
        self.clock = clock
        self._cache: dict = {}
        self._journal = None

    def reset(self) -> None:
        self._cache.clear()

    def start_background_recording(self) -> None:
        """Kubernetes keeps only a container's latest termination; the pod journal records every one."""
        if self.journal_path is None or self._journal is not None:
            return
        import time

        from ..kube import PodJournal
        self._journal = PodJournal(self.kube, self.ns, self.journal_path, clock=self.clock or time.time)
        self._journal.start()

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
        history = []
        for e in read_journal(self.journal_path, time_range.start - 60, time_range.end + 60) if self.journal_path else []:
            if e["kind"] == "container_terminated" and e.get("app") and e["pod"] in names:
                history.append(TerminationRecord(
                    instance=e["pod"], process=e.get("container"), restarts=e.get("restart_count"),
                    termination=Termination(e.get("reason"), e.get("exit_code"), e.get("started_at"), e["t"],
                                            termination_cause(e.get("reason"), e.get("exit_code"))),
                    source="kubernetes.pod_journal"))
        return ResourceState(
            component=w["name"], kind=w["kind"], scope=w["namespace"], desired=w["replicas_desired"],
            ready=w["replicas_ready"], available=w["replicas_available"],
            limits={c["name"]: c["limits"] for c in w["containers"]},
            instances=[_instance(p) for p in pods], history=history)

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
