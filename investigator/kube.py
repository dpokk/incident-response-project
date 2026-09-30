"""Kubernetes API access: read-only snapshots, events, logs, a pod-state journal and fixed probes.

Nothing in this module modifies cluster state. The only exec is `probe_tcp`, a fixed DNS+TCP check
(validated host/port, no shell) used to test dependency connectivity from inside a consumer pod.
"""
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream


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


_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$")

# Fixed probe program: resolve the host, then try a TCP connection. Arguments are passed via argv.
_PROBE_SRC = (
    "import json,socket,sys,time\n"
    "h,p=sys.argv[1],int(sys.argv[2]);r={'host':h,'port':p}\n"
    "try:\n r['addresses']=sorted({a[4][0] for a in socket.getaddrinfo(h,p,proto=socket.IPPROTO_TCP)});r['dns']='ok'\n"
    "except Exception as e:\n r['dns']='error';r['dns_error']=str(e)\n"
    "if r['dns']=='ok':\n"
    " t=time.time()\n"
    " try:\n  s=socket.create_connection((h,p),timeout=3);s.close();r['tcp']='ok'\n"
    " except ConnectionRefusedError as e:\n  r['tcp']='refused';r['tcp_error']=str(e)\n"
    " except socket.timeout as e:\n  r['tcp']='timeout';r['tcp_error']='timed out'\n"
    " except Exception as e:\n  r['tcp']='error';r['tcp_error']=str(e)\n"
    " r['tcp_ms']=round((time.time()-t)*1000)\n"
    "print(json.dumps(r))\n"
)


class Kube:
    def __init__(self, context: str = ""):
        config.load_kube_config(context=context or None)
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()

    # -- workloads ----------------------------------------------------------
    def namespace(self, ns: str) -> dict:
        n = self.core.read_namespace(ns)
        return {"name": ns, "phase": n.status.phase, "labels": n.metadata.labels or {}}

    def pods(self, ns: str) -> list[dict]:
        return [pod_summary(p) for p in self.core.list_namespaced_pod(ns).items]

    def workloads(self, ns: str) -> list[dict]:
        """Deployments and StatefulSets with their pod template (containers, env, resources, probes)."""
        out = []
        items = [("Deployment", d) for d in self.apps.list_namespaced_deployment(ns).items]
        items += [("StatefulSet", s) for s in self.apps.list_namespaced_stateful_set(ns).items]
        for kind, w in items:
            st = w.status
            out.append({
                "kind": kind, "name": w.metadata.name, "namespace": ns,
                "labels": w.metadata.labels or {},
                "selector": w.spec.selector.match_labels or {},
                "replicas_desired": w.spec.replicas if w.spec.replicas is not None else 1,
                "replicas_ready": st.ready_replicas or 0,
                "replicas_available": getattr(st, "available_replicas", None) or 0,
                "replicas_current": st.replicas or 0,
                "generation": w.metadata.generation,
                "revision": (w.metadata.annotations or {}).get("deployment.kubernetes.io/revision"),
                "conditions": [{"type": c.type, "status": c.status, "reason": c.reason, "message": c.message,
                                "last_update": ts(getattr(c, "last_update_time", None)),
                                "last_transition": ts(c.last_transition_time)}
                               for c in (st.conditions or [])],
                "containers": [_container_spec(c) for c in w.spec.template.spec.containers],
            })
        return out

    def replicasets(self, ns: str) -> list[dict]:
        out = []
        for rs in self.apps.list_namespaced_replica_set(ns).items:
            owner = next((o.name for o in (rs.metadata.owner_references or []) if o.kind == "Deployment"), None)
            out.append({"name": rs.metadata.name, "deployment": owner, "created": ts(rs.metadata.creation_timestamp),
                        "revision": (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision"),
                        "replicas": rs.spec.replicas, "ready": rs.status.ready_replicas or 0})
        return out

    # -- networking ---------------------------------------------------------
    def services(self, ns: str | None = None) -> list[dict]:
        items = (self.core.list_namespaced_service(ns) if ns else self.core.list_service_for_all_namespaces()).items
        return [{"name": s.metadata.name, "namespace": s.metadata.namespace, "type": s.spec.type,
                 "cluster_ip": s.spec.cluster_ip, "selector": s.spec.selector or {},
                 "ports": [{"name": p.name, "port": p.port, "target": str(p.target_port)} for p in (s.spec.ports or [])]}
                for s in items]

    def endpoints(self, ns: str, name: str) -> dict:
        try:
            ep = self.core.read_namespaced_endpoints(name, ns)
        except ApiException as exc:
            if exc.status == 404:
                return {"exists": False, "ready": [], "not_ready": []}
            raise

        def addrs(lst):
            return [{"ip": a.ip, "pod": a.target_ref.name if a.target_ref else None} for a in (lst or [])]
        ready, not_ready = [], []
        for sub in ep.subsets or []:
            ready += addrs(sub.addresses)
            not_ready += addrs(sub.not_ready_addresses)
        return {"exists": True, "ready": ready, "not_ready": not_ready}

    # -- configuration --------------------------------------------------------
    def configmaps(self, ns: str) -> list[dict]:
        out = []
        for cm in self.core.list_namespaced_config_map(ns).items:
            if cm.metadata.name == "kube-root-ca.crt":
                continue
            times = [ts(m.time) for m in (cm.metadata.managed_fields or []) if m.time]
            out.append({"name": cm.metadata.name, "data": cm.data or {}, "created": ts(cm.metadata.creation_timestamp),
                        "last_modified": max(times) if times else ts(cm.metadata.creation_timestamp)})
        return out

    def secret_values(self, ns: str, name: str) -> dict:
        """Decoded secret data. Callers must never report the values, only derived facts (e.g. a hostname)."""
        import base64
        try:
            s = self.core.read_namespaced_secret(name, ns)
        except ApiException:
            return {}
        return {k: base64.b64decode(v).decode(errors="replace") for k, v in (s.data or {}).items()}

    # -- events & logs --------------------------------------------------------
    def events(self, ns: str) -> list[dict]:
        out = []
        for e in self.core.list_namespaced_event(ns).items:
            first = ts(e.first_timestamp) or ts(e.event_time) or ts(e.metadata.creation_timestamp)
            last = ts(e.last_timestamp) or (ts(e.series.last_observed_time) if e.series else None) or first
            out.append({"type": e.type, "reason": e.reason, "message": (e.message or "").strip(),
                        "object_kind": e.involved_object.kind, "object_name": e.involved_object.name,
                        "count": e.count or (e.series.count if e.series else 1) or 1, "first": first, "last": last})
        return sorted(out, key=lambda x: x["first"] or 0)

    def node_events(self) -> list[dict]:
        return [{"type": e.type, "reason": e.reason, "message": (e.message or "").strip(),
                 "object_name": e.involved_object.name,
                 "first": ts(e.first_timestamp) or ts(e.event_time), "last": ts(e.last_timestamp)}
                for e in self.core.list_event_for_all_namespaces(field_selector="involvedObject.kind=Node").items]

    def logs(self, ns: str, pod: str, container: str, since_s: int | None = None,
             previous: bool = False, tail: int = 2000) -> list[tuple[float | None, str]]:
        """Log lines with the runtime's timestamp (works for JSON and plain-text lines alike)."""
        kwargs = {"container": container, "tail_lines": tail, "previous": previous, "timestamps": True,
                  "_preload_content": False}  # raw bytes: some client versions mangle the decoded string
        if since_s and not previous:
            kwargs["since_seconds"] = max(1, int(since_s))
        try:
            resp = self.core.read_namespaced_pod_log(pod, ns, **kwargs)
            text = resp.data.decode("utf-8", errors="replace")
        except ApiException:
            return []
        out = []
        for line in text.splitlines():
            stamp, _, rest = line.partition(" ")
            try:
                out.append((ts(stamp[:26] + "Z" if len(stamp) > 27 else stamp), rest))
            except ValueError:
                out.append((None, line))
        return out

    # -- probes (read-only) ---------------------------------------------------------
    def service_proxy_get(self, ns: str, service: str, port: str, path: str, timeout: float = 10) -> tuple[int, str]:
        """HTTP GET through the API server's service proxy, i.e. a synthetic user request."""
        path, _, qs = path.lstrip("/").partition("?")
        query = [tuple(kv.split("=", 1)) for kv in qs.split("&") if "=" in kv]
        try:
            result = self.core.api_client.call_api(
                f"/api/v1/namespaces/{ns}/services/{service}:{port}/proxy/{path}", "GET",
                query_params=query, header_params={"Accept": "*/*"}, auth_settings=["BearerToken"],
                _preload_content=False, _request_timeout=timeout)
            resp = result[0] if isinstance(result, tuple) else result  # shape differs across client versions
            return resp.status, resp.data.decode(errors="replace")[:500]
        except ApiException as exc:
            body = exc.body.decode(errors="replace") if isinstance(exc.body, bytes) else str(exc.body or exc.reason)
            return exc.status or 0, body[:500]
        except Exception as exc:  # noqa: BLE001 - timeouts / connection errors count as probe failures
            return 0, f"{type(exc).__name__}: {exc}"[:300]

    def probe_tcp(self, ns: str, pod: str, container: str, host: str, port: int, timeout: float = 20) -> dict:
        """Resolve `host` and open a TCP connection to it from inside `pod` (fixed program, no shell)."""
        if not _HOST_RE.match(host) or not (0 < int(port) < 65536):
            return {"error": "invalid host/port"}
        try:
            ws = stream(self.core.connect_get_namespaced_pod_exec, pod, ns, container=container,
                        command=["python", "-c", _PROBE_SRC, host, str(int(port))],
                        stderr=True, stdin=False, stdout=True, tty=False, _preload_content=False)
            ws.run_forever(timeout=timeout)
            out = ws.read_stdout() or ""
            ws.close()
            line = next((l for l in out.splitlines() if l.strip().startswith("{")), "")
            if not line:
                return {"error": f"no probe output: {out[:200]}"}
            try:
                return json.loads(line)
            except json.JSONDecodeError:  # some client versions hand back a Python repr
                import ast
                return ast.literal_eval(line)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"[:300]}


def _probe(p) -> dict | None:
    if not p:
        return None
    kind = "http" if p.http_get else "exec" if p._exec else "tcp" if p.tcp_socket else "other"
    return {"type": kind, "path": p.http_get.path if p.http_get else None, "period_s": p.period_seconds,
            "failure_threshold": p.failure_threshold}


def _container_spec(c) -> dict:
    res = c.resources
    env = []
    for e in c.env or []:
        src = None
        if e.value_from:
            if e.value_from.config_map_key_ref:
                src = {"configmap": e.value_from.config_map_key_ref.name, "key": e.value_from.config_map_key_ref.key}
            elif e.value_from.secret_key_ref:
                src = {"secret": e.value_from.secret_key_ref.name, "key": e.value_from.secret_key_ref.key}
            elif e.value_from.field_ref:
                src = {"field": e.value_from.field_ref.field_path}
        env.append({"name": e.name, "value": e.value, "from": src})
    return {
        "name": c.name, "image": c.image, "command": c.command,
        "requests": dict(res.requests or {}) if res else {},
        "limits": dict(res.limits or {}) if res else {},
        "env": env,
        "env_from": [{"configmap": ef.config_map_ref.name} if ef.config_map_ref else {"secret": ef.secret_ref.name}
                     for ef in (c.env_from or []) if ef.config_map_ref or ef.secret_ref],
        "readiness_probe": _probe(c.readiness_probe), "liveness_probe": _probe(c.liveness_probe),
    }


def _state(s) -> dict | None:
    if s is None:
        return None
    if s.running:
        return {"state": "running", "started_at": ts(s.running.started_at)}
    if s.waiting:
        return {"state": "waiting", "reason": s.waiting.reason, "message": s.waiting.message}
    if s.terminated:
        t = s.terminated
        return {"state": "terminated", "reason": t.reason, "exit_code": t.exit_code, "signal": t.signal,
                "message": t.message, "started_at": ts(t.started_at), "finished_at": ts(t.finished_at)}
    return None


def pod_summary(p) -> dict:
    ready_cond = next((c for c in (p.status.conditions or []) if c.type == "Ready"), None)
    sched_cond = next((c for c in (p.status.conditions or []) if c.type == "PodScheduled"), None)
    spec_containers = {c.name: c for c in p.spec.containers}
    containers = []
    for cs in p.status.container_statuses or []:
        spec = spec_containers.get(cs.name)
        res = spec.resources if spec else None
        containers.append({
            "name": cs.name, "image": cs.image, "ready": cs.ready, "restart_count": cs.restart_count,
            "state": _state(cs.state), "last_state": _state(cs.last_state),
            "cpu_limit_cores": parse_cpu((res.limits or {}).get("cpu")) if res and res.limits else None,
            "memory_limit_bytes": parse_mem((res.limits or {}).get("memory")) if res and res.limits else None,
        })
    return {
        "name": p.metadata.name, "namespace": p.metadata.namespace,
        "app": (p.metadata.labels or {}).get("app"), "labels": p.metadata.labels or {},
        "owner": next((o.name for o in (p.metadata.owner_references or [])), None),
        "node": p.spec.node_name, "phase": p.status.phase, "pod_ip": p.status.pod_ip,
        "created": ts(p.metadata.creation_timestamp), "deleted": ts(p.metadata.deletion_timestamp),
        "ready": bool(ready_cond and ready_cond.status == "True"),
        "ready_since": ts(ready_cond.last_transition_time) if ready_cond else None,
        "unschedulable": bool(sched_cond and sched_cond.status == "False"),
        "containers": containers,
    }


class PodJournal:
    """Watches pods and records every state transition. Kubernetes only keeps the last termination of
    each container, so a crash-looping pod loses history; the journal preserves each one."""

    def __init__(self, kube: Kube, ns: str, path: Path, clock=time.time):
        self.kube, self.ns, self.path, self.clock = kube, ns, path, clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.prev: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.started_at = clock()
        self._stop = threading.Event()

    def start(self) -> None:
        for p in self.kube.pods(self.ns):
            self.prev[p["name"]] = p
        threading.Thread(target=self._run, daemon=True, name="pod-journal").start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                w = watch.Watch()
                for ev in w.stream(self.kube.core.list_namespaced_pod, self.ns, timeout_seconds=60):
                    self._on(ev["type"], pod_summary(ev["object"]))
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
            self._write({**base, "t": now, "kind": "pod_deleted"})
            self.prev.pop(name, None)
            return
        self.prev[name] = pod
        if prev is None:
            if (pod["created"] or 0) >= self.started_at - 5:
                self._write({**base, "t": pod["created"] or now, "kind": "pod_created"})
            return
        if pod["ready"] != prev["ready"]:
            self._write({**base, "t": pod["ready_since"] or now, "kind": "pod_ready" if pod["ready"] else "pod_not_ready"})
        prev_c = {c["name"]: c for c in prev["containers"]}
        for c in pod["containers"]:
            pc = prev_c.get(c["name"])
            if not pc:
                continue
            if c["restart_count"] > pc["restart_count"]:
                term = c["last_state"] or {}
                self._write({**base, "t": term.get("finished_at") or now, "kind": "container_terminated",
                             "container": c["name"], "reason": term.get("reason"), "exit_code": term.get("exit_code"),
                             "started_at": term.get("started_at"), "restart_count": c["restart_count"]})
            st, pst = c["state"] or {}, pc["state"] or {}
            if st.get("state") == "waiting" and st.get("reason") != pst.get("reason"):
                self._write({**base, "t": now, "kind": "container_waiting", "container": c["name"],
                             "reason": st.get("reason"), "message": (st.get("message") or "")[:300]})


def read_journal(path: Path, start: float, end: float) -> list[dict]:
    out, seen = [], set()
    if not path or not path.exists():
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
