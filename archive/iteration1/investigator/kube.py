"""Kubernetes API access: resource snapshots, events, logs and a pod-state journal."""
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException


def ts(dt) -> float | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


_MEM_UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}


def parse_cpu(v) -> float | None:
    if v is None:
        return None
    v = str(v)
    return float(v[:-1]) / 1000 if v.endswith("m") else float(v)


def parse_mem(v) -> float | None:
    if v is None:
        return None
    m = re.fullmatch(r"([0-9.]+)([A-Za-z]*)", str(v))
    if not m:
        return None
    return float(m.group(1)) * _MEM_UNITS.get(m.group(2), 1)


class Kube:
    def __init__(self, context: str = ""):
        config.load_kube_config(context=context or None)
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()

    # -- snapshots ----------------------------------------------------------
    def namespace(self, ns: str) -> dict:
        n = self.core.read_namespace(ns)
        return {"name": ns, "phase": n.status.phase, "labels": n.metadata.labels or {},
                "created": ts(n.metadata.creation_timestamp)}

    def pods(self, ns: str) -> list[dict]:
        return [pod_summary(p) for p in self.core.list_namespaced_pod(ns).items]

    def deployments(self, ns: str) -> list[dict]:
        out = []
        for d in self.apps.list_namespaced_deployment(ns).items:
            containers = []
            for c in d.spec.template.spec.containers:
                res = c.resources
                env = {e.name: e.value for e in (c.env or []) if e.value is not None}
                containers.append({
                    "name": c.name, "image": c.image, "command": c.command,
                    "requests": dict(res.requests or {}) if res else {},
                    "limits": dict(res.limits or {}) if res else {},
                    "env": env,
                    "env_from_configmaps": [ef.config_map_ref.name for ef in (c.env_from or []) if ef.config_map_ref],
                    "readiness_probe": _probe(c.readiness_probe),
                    "liveness_probe": _probe(c.liveness_probe),
                })
            out.append({
                "name": d.metadata.name,
                "labels": d.metadata.labels or {},
                "selector": d.spec.selector.match_labels or {},
                "replicas_desired": d.spec.replicas,
                "replicas_ready": d.status.ready_replicas or 0,
                "replicas_available": d.status.available_replicas or 0,
                "replicas_unavailable": d.status.unavailable_replicas or 0,
                "generation": d.metadata.generation,
                "revision": (d.metadata.annotations or {}).get("deployment.kubernetes.io/revision"),
                "strategy": d.spec.strategy.type if d.spec.strategy else None,
                "conditions": [{"type": c.type, "status": c.status, "reason": c.reason, "message": c.message,
                                "last_update": ts(c.last_update_time), "last_transition": ts(c.last_transition_time)}
                               for c in (d.status.conditions or [])],
                "containers": containers,
            })
        return out

    def replicasets(self, ns: str) -> list[dict]:
        out = []
        for rs in self.apps.list_namespaced_replica_set(ns).items:
            owner = next((o.name for o in (rs.metadata.owner_references or []) if o.kind == "Deployment"), None)
            out.append({
                "name": rs.metadata.name, "deployment": owner, "created": ts(rs.metadata.creation_timestamp),
                "revision": (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision"),
                "replicas": rs.spec.replicas, "ready": rs.status.ready_replicas or 0,
                "images": [c.image for c in rs.spec.template.spec.containers],
            })
        return out

    def services(self, ns: str) -> list[dict]:
        out = []
        for s in self.core.list_namespaced_service(ns).items:
            try:
                ep = self.core.read_namespaced_endpoints(s.metadata.name, ns)
                ready = sum(len(sub.addresses or []) for sub in (ep.subsets or []))
                not_ready = sum(len(sub.not_ready_addresses or []) for sub in (ep.subsets or []))
            except ApiException:
                ready = not_ready = None
            out.append({
                "name": s.metadata.name, "type": s.spec.type, "cluster_ip": s.spec.cluster_ip,
                "selector": s.spec.selector or {},
                "ports": [{"port": p.port, "target": str(p.target_port)} for p in (s.spec.ports or [])],
                "endpoints_ready": ready, "endpoints_not_ready": not_ready,
            })
        return out

    def configmaps(self, ns: str) -> list[dict]:
        out = []
        for cm in self.core.list_namespaced_config_map(ns).items:
            if cm.metadata.name == "kube-root-ca.crt":
                continue
            times = [ts(m.time) for m in (cm.metadata.managed_fields or []) if m.time]
            out.append({"name": cm.metadata.name, "data": cm.data or {},
                        "created": ts(cm.metadata.creation_timestamp),
                        "last_modified": max(times) if times else ts(cm.metadata.creation_timestamp)})
        return out

    def events(self, ns: str) -> list[dict]:
        out = []
        for e in self.core.list_namespaced_event(ns).items:
            first = ts(e.first_timestamp) or ts(e.event_time) or ts(e.metadata.creation_timestamp)
            last = ts(e.last_timestamp) or (ts(e.series.last_observed_time) if e.series else None) or first
            out.append({
                "type": e.type, "reason": e.reason, "message": (e.message or "").strip(),
                "object_kind": e.involved_object.kind, "object_name": e.involved_object.name,
                "field_path": e.involved_object.field_path,
                "count": e.count or (e.series.count if e.series else 1) or 1,
                "first": first, "last": last,
                "source": e.source.component if e.source else e.reporting_component,
            })
        return sorted(out, key=lambda x: x["first"] or 0)

    def node_events(self) -> list[dict]:
        out = []
        for e in self.core.list_event_for_all_namespaces(field_selector="involvedObject.kind=Node").items:
            out.append({"type": e.type, "reason": e.reason, "message": (e.message or "").strip(),
                        "object_name": e.involved_object.name,
                        "first": ts(e.first_timestamp) or ts(e.event_time), "last": ts(e.last_timestamp),
                        "count": e.count or 1})
        return out

    def logs(self, ns: str, pod: str, container: str, since_s: int | None = None,
             previous: bool = False, tail: int = 3000) -> list[str]:
        kwargs = {"container": container, "tail_lines": tail, "previous": previous}
        if since_s and not previous:
            kwargs["since_seconds"] = max(1, int(since_s))
        try:
            text = self.core.read_namespaced_pod_log(pod, ns, **kwargs)
        except ApiException:
            return []
        return text.splitlines()


def _probe(p) -> dict | None:
    if not p:
        return None
    return {"path": p.http_get.path if p.http_get else None, "period_s": p.period_seconds,
            "timeout_s": p.timeout_seconds, "failure_threshold": p.failure_threshold,
            "initial_delay_s": p.initial_delay_seconds}


def _state(s) -> dict | None:
    if s is None:
        return None
    if s.running:
        return {"state": "running", "started_at": ts(s.running.started_at)}
    if s.waiting:
        return {"state": "waiting", "reason": s.waiting.reason, "message": s.waiting.message}
    if s.terminated:
        t = s.terminated
        return {"state": "terminated", "reason": t.reason, "exit_code": t.exit_code,
                "started_at": ts(t.started_at), "finished_at": ts(t.finished_at)}
    return None


def pod_summary(p) -> dict:
    ready_cond = next((c for c in (p.status.conditions or []) if c.type == "Ready"), None)
    spec_containers = {c.name: c for c in p.spec.containers}
    containers = []
    for cs in p.status.container_statuses or []:
        spec = spec_containers.get(cs.name)
        res = spec.resources if spec else None
        containers.append({
            "name": cs.name, "ready": cs.ready, "restart_count": cs.restart_count,
            "state": _state(cs.state), "last_state": _state(cs.last_state),
            "cpu_limit_cores": parse_cpu((res.limits or {}).get("cpu")) if res and res.limits else None,
            "memory_limit_bytes": parse_mem((res.limits or {}).get("memory")) if res and res.limits else None,
        })
    return {
        "name": p.metadata.name,
        "app": (p.metadata.labels or {}).get("app"),
        "owner": next((o.name for o in (p.metadata.owner_references or [])), None),
        "node": p.spec.node_name, "phase": p.status.phase, "pod_ip": p.status.pod_ip,
        "created": ts(p.metadata.creation_timestamp),
        "deleted": ts(p.metadata.deletion_timestamp),
        "ready": bool(ready_cond and ready_cond.status == "True"),
        "ready_since": ts(ready_cond.last_transition_time) if ready_cond else None,
        "containers": containers,
    }


class PodJournal:
    """Watches pods in a namespace and records every state transition with its timestamp.

    Kubernetes only keeps the *last* termination of each container, so a crash-looping pod
    loses history. The journal preserves each OOMKill/restart/readiness flip as it happens
    and is persisted to disk so later investigations can use it.
    """

    def __init__(self, kube: Kube, ns: str, path: Path, clock=time.time):
        self.kube, self.ns, self.path, self.clock = kube, ns, path, clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.prev: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.started_at = clock()
        self._stop = threading.Event()

    def start(self) -> None:
        # Seed with current state so the initial watch ADDED events don't count as changes.
        for p in self.kube.pods(self.ns):
            self.prev[p["name"]] = p
        threading.Thread(target=self._run, daemon=True, name="pod-journal").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                w = watch.Watch()
                for ev in w.stream(self.kube.core.list_namespaced_pod, self.ns, timeout_seconds=60):
                    self._on(ev["type"], pod_summary(ev["object"]))
                    if self._stop.is_set():
                        break
            except Exception:  # noqa: BLE001 - watch reconnects on any API hiccup
                time.sleep(2)

    def _write(self, entry: dict) -> None:
        with self.lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _on(self, etype: str, pod: dict) -> None:
        name, now = pod["name"], self.clock()
        prev = self.prev.get(name)
        base = {"namespace": self.ns, "pod": name, "app": pod["app"]}
        if etype == "DELETED":
            self._write({**base, "t": now, "kind": "pod_deleted", "observed": True})
            self.prev.pop(name, None)
            return
        self.prev[name] = pod
        if prev is None:
            if (pod["created"] or 0) >= self.started_at - 5:
                self._write({**base, "t": pod["created"] or now, "kind": "pod_created", "node": pod["node"]})
            return
        if pod["ready"] != prev["ready"]:
            self._write({**base, "t": pod["ready_since"] or now,
                         "kind": "pod_ready" if pod["ready"] else "pod_not_ready"})
        prev_c = {c["name"]: c for c in prev["containers"]}
        for c in pod["containers"]:
            pc = prev_c.get(c["name"])
            if not pc:
                continue
            if c["restart_count"] > pc["restart_count"]:
                term = c["last_state"] or {}
                self._write({**base, "t": term.get("finished_at") or now, "kind": "container_terminated",
                             "container": c["name"], "reason": term.get("reason"),
                             "exit_code": term.get("exit_code"), "started_at": term.get("started_at"),
                             "restart_count": c["restart_count"],
                             "memory_limit_bytes": c["memory_limit_bytes"]})
            st, pst = c["state"] or {}, pc["state"] or {}
            if st.get("state") == "waiting" and st.get("reason") != pst.get("reason"):
                self._write({**base, "t": now, "kind": "container_waiting", "container": c["name"],
                             "reason": st.get("reason"), "message": st.get("message"), "observed": True})
            if st.get("state") == "running" and st.get("started_at") != pst.get("started_at"):
                self._write({**base, "t": st.get("started_at") or now, "kind": "container_started",
                             "container": c["name"], "restart_count": c["restart_count"]})

    def between(self, start: float, end: float) -> list[dict]:
        if not self.path.exists():
            return []
        return read_journal(self.path, start, end)


def read_journal(path: Path, start: float, end: float) -> list[dict]:
    out, seen = [], set()
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (e.get("pod"), e.get("kind"), e.get("container"), round(e.get("t") or 0, 1))
            if start <= (e.get("t") or 0) <= end and key not in seen:
                seen.add(key)
                out.append(e)
    return sorted(out, key=lambda e: e["t"])
