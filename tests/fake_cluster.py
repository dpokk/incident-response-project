"""A fake Kubernetes API returning realistic *raw* cluster state for offline tests.

Each "world" describes what the cluster looks like after a failure. The investigator is run against
it exactly as against the real API; it is never told which world it is looking at.
"""
import copy
import json
import time

NOW = time.time()
NS = "shop"


def _pod(name, app, ready=True, restarts=0, state=None, last=None, mem_limit=192 * 2**20, cpu=0.5, phase="Running"):
    return {"name": name, "namespace": NS, "app": app, "labels": {"app": app}, "owner": f"{app}-rs",
            "node": "node1", "phase": phase, "pod_ip": "10.0.0.1", "created": NOW - 3600, "deleted": None,
            "ready": ready, "ready_since": NOW - (30 if not ready else 3000), "unschedulable": False,
            "containers": [{"name": app, "image": f"{app}:1", "ready": ready, "restart_count": restarts,
                            "state": state or {"state": "running", "started_at": NOW - 3000}, "last_state": last,
                            "cpu_limit_cores": cpu, "memory_limit_bytes": mem_limit}]}


def _workload(name, desired, ready, env=None, env_from=None, limits=None):
    return {"kind": "Deployment", "name": name, "namespace": NS, "labels": {"app": name}, "selector": {"app": name},
            "replicas_desired": desired, "replicas_ready": ready, "replicas_available": ready, "replicas_current": ready,
            "generation": 1, "revision": "1", "conditions": [],
            "containers": [{"name": name, "image": f"{name}:1", "command": None, "requests": {},
                            "limits": limits or {}, "env": env or [], "env_from": env_from or [],
                            "readiness_probe": None, "liveness_probe": None}]}


def _j(**rec):
    rec.setdefault("ts", "")
    return json.dumps(rec)


def base_world() -> dict:
    return {
        "workloads": [
            _workload("frontend", 1, 1, env=[{"name": "BACKEND_URL", "value": "http://backend.shop.svc.cluster.local:8080", "from": None}]),
            _workload("backend", 2, 2, env=[{"name": "PGPASSWORD", "value": None, "from": {"secret": "postgres-credentials", "key": "POSTGRES_PASSWORD"}}],
                      env_from=[{"configmap": "backend-config"}], limits={"cpu": "500m", "memory": "192Mi"}),
            _workload("postgres", 1, 1, env_from=[{"secret": "postgres-credentials"}]),
        ],
        "replicasets": [{"name": "backend-rs", "deployment": "backend", "created": NOW - 3600, "revision": "1", "replicas": 2, "ready": 2}],
        "pods": [_pod("frontend-aaaaa", "frontend", mem_limit=512 * 2**20, cpu=None), _pod("backend-11111", "backend"),
                 _pod("backend-22222", "backend"), _pod("postgres-33333", "postgres", mem_limit=512 * 2**20, cpu=None)],
        "services": [{"name": n, "namespace": NS, "type": "ClusterIP", "cluster_ip": f"10.96.0.{i}", "selector": {"app": n},
                      "ports": [{"name": "p", "port": port, "target": "p"}]}
                     for i, (n, port) in enumerate([("frontend", 8080), ("backend", 8080), ("postgres", 5432)], 10)],
        "endpoints": {"frontend": 1, "backend": 2, "postgres": 1},
        "configmaps": [{"name": "backend-config", "data": {"DATABASE_URL": "postgresql://shop@postgres:5432/shop", "CPU_WORK_MS": "1.5"},
                        "created": NOW - 3600, "last_modified": NOW - 3600}],
        "secrets": {"postgres-credentials": {"POSTGRES_PASSWORD": "s3cret", "POSTGRES_USER": "shop", "POSTGRES_DB": "shop"}},
        "events": [],
        "logs": {},       # (pod, previous) -> [(t, line)]
        "probes": {"postgres": {"dns": "ok", "addresses": ["10.96.0.12"], "tcp": "ok", "tcp_ms": 2},
                   "backend.shop.svc.cluster.local": {"dns": "ok", "addresses": ["10.96.0.11"], "tcp": "ok", "tcp_ms": 1}},
        "entry": (200, '{"status": "accepted"}'),
    }


def world_oom() -> dict:
    w = base_world()
    last = {"state": "terminated", "reason": "OOMKilled", "exit_code": 137, "signal": None, "message": None,
            "started_at": NOW - 80, "finished_at": NOW - 60}
    w["pods"][1] = _pod("backend-11111", "backend", ready=False, restarts=3, last=last)
    w["pods"][2] = _pod("backend-22222", "backend", ready=False, restarts=4, last={**last, "finished_at": NOW - 50})
    w["endpoints"]["backend"] = 0
    w["events"] = [{"type": "Warning", "reason": "BackOff", "message": "Back-off restarting failed container backend",
                    "object_kind": "Pod", "object_name": "backend-11111", "count": 2, "first": NOW - 55, "last": NOW - 20}]
    for pod in ("backend-11111", "backend-22222"):
        w["logs"][(pod, True)] = [(NOW - 70, _j(level="warning", msg="request backlog growing; workers cannot keep up", inflight=400)),
                                  (NOW - 65, _j(level="warning", msg="memory usage approaching container limit", mem_limit_ratio=0.93))]
    w["logs"][("frontend-aaaaa", False)] = [(NOW - 60 + i, _j(level="error", msg="upstream request to backend failed",
                                                              upstream="http://backend.shop.svc.cluster.local:8080",
                                                              reason="upstream_timeout", count=300,
                                                              sample_error="no response from backend within 3.0s"))
                                            for i in range(3)]
    w["entry"] = (504, '{"error": "upstream timeout"}')
    return w


def world_db_misconfig() -> dict:
    w = base_world()
    w["configmaps"][0]["data"]["DATABASE_URL"] = "postgresql://shop@postgres-wrong:5432/shop"
    w["configmaps"][0]["last_modified"] = NOW - 200
    w["replicasets"].append({"name": "backend-rs2", "deployment": "backend", "created": NOW - 195, "revision": "2", "replicas": 2, "ready": 2})
    err = "failed to resolve host 'postgres-wrong': [Errno -2] Name or service not known"
    for pod in ("backend-11111", "backend-22222"):
        w["logs"][(pod, False)] = [(NOW - 180 + i * 10, _j(level="error", msg="database connection failed", db_host="postgres-wrong",
                                                           db_port="5432", error_type="OperationalError", error=err))
                                   for i in range(12)]
        w["logs"][(pod, False)] += [(NOW - 170, _j(level="error", msg="requests failed: database unavailable", count=90,
                                                   db_host="postgres-wrong", db_port="5432", last_error=err))]
    w["logs"][("frontend-aaaaa", False)] = [(NOW - 150, _j(level="error", msg="upstream request to backend failed",
                                                           upstream="http://backend.shop.svc.cluster.local:8080",
                                                           reason="upstream_http_503", count=500,
                                                           sample_error='{"error": "database unavailable"}'))]
    w["probes"]["postgres-wrong"] = {"dns": "error", "dns_error": "[Errno -2] Name or service not known"}
    w["entry"] = (503, '{"error": "database unavailable"}')
    return w


def world_db_down() -> dict:
    w = base_world()
    w["workloads"][2].update(replicas_desired=0, replicas_ready=0, replicas_available=0)
    w["pods"] = w["pods"][:3]
    w["endpoints"]["postgres"] = 0
    w["events"] = [{"type": "Normal", "reason": "ScalingReplicaSet", "message": "Scaled down replica set postgres-rs from 1 to 0",
                    "object_kind": "Deployment", "object_name": "postgres", "count": 1, "first": NOW - 200, "last": NOW - 200},
                   {"type": "Normal", "reason": "Killing", "message": "Stopping container postgres",
                    "object_kind": "Pod", "object_name": "postgres-33333", "count": 1, "first": NOW - 199, "last": NOW - 199}]
    err = 'connection failed: connection to server at "10.96.0.12", port 5432 failed: Connection refused'
    for pod in ("backend-11111", "backend-22222"):
        w["logs"][(pod, False)] = [(NOW - 190, _j(level="error", msg="database connection failed", db_host="postgres", db_port="5432",
                                                  error_type="OperationalError", error="server closed the connection unexpectedly"))]
        w["logs"][(pod, False)] += [(NOW - 180 + i * 10, _j(level="error", msg="database connection failed", db_host="postgres",
                                                            db_port="5432", error_type="OperationalError", error=err))
                                    for i in range(12)]
    w["logs"][("frontend-aaaaa", False)] = [(NOW - 150, _j(level="error", msg="upstream request to backend failed",
                                                           upstream="http://backend.shop.svc.cluster.local:8080",
                                                           reason="upstream_http_503", count=500,
                                                           sample_error='{"error": "database unavailable"}'))]
    w["probes"]["postgres"] = {"dns": "ok", "addresses": ["10.96.0.12"], "tcp": "refused", "tcp_error": "[Errno 111] Connection refused"}
    w["entry"] = (503, '{"error": "database unavailable"}')
    return w


def world_crash() -> dict:
    w = base_world()
    last = {"state": "terminated", "reason": "Error", "exit_code": 1, "signal": None, "message": None,
            "started_at": NOW - 40, "finished_at": NOW - 35}
    waiting = {"state": "waiting", "reason": "CrashLoopBackOff", "message": "back-off 40s restarting failed container=backend"}
    w["pods"][1] = _pod("backend-11111", "backend", ready=False, restarts=4, state=waiting, last=last)
    w["pods"][2] = _pod("backend-22222", "backend", ready=False, restarts=4, state=waiting, last={**last, "finished_at": NOW - 30})
    w["endpoints"]["backend"] = 0
    w["events"] = [{"type": "Warning", "reason": "BackOff", "message": "Back-off restarting failed container backend in pod backend-11111",
                    "object_kind": "Pod", "object_name": "backend-11111", "count": 6, "first": NOW - 150, "last": NOW - 20}]
    tb = ['Traceback (most recent call last):',
          '  File "/app/backend.py", line 380, in <module>',
          '    main()',
          '  File "/usr/local/lib/python3.12/asyncio/runners.py", line 194, in run',
          '    return runner.run(main)',
          '  File "/app/backend.py", line 300, in invoice_batch',
          '    unit_price = total / quantity',
          '                 ~~~~~~^~~~~~~~~~',
          "decimal.DivisionByZero: [<class 'decimal.DivisionByZero'>]"]
    for pod in ("backend-11111", "backend-22222"):
        w["logs"][(pod, True)] = [(NOW - 40, _j(level="info", msg="backend starting")),
                                  (NOW - 39, _j(level="info", msg="database connection established", db_host="postgres"))]
        w["logs"][(pod, True)] += [(NOW - 36, line) for line in tb]
    w["logs"][("frontend-aaaaa", False)] = [(NOW - 100, _j(level="error", msg="upstream request to backend failed",
                                                           upstream="http://backend.shop.svc.cluster.local:8080",
                                                           reason="upstream_connect_error", count=400,
                                                           sample_error="Cannot connect to host backend.shop.svc.cluster.local:8080 ssl:default [Connect call failed ('10.96.0.11', 8080)] Connection refused"))]
    w["probes"]["backend.shop.svc.cluster.local"] = {"dns": "ok", "addresses": ["10.96.0.11"], "tcp": "refused", "tcp_error": "refused"}
    w["entry"] = (503, '{"error": "backend unavailable"}')
    return w


class FakeKube:
    """Implements the subset of investigator.kube.Kube used by the toolset, from a world dict."""

    def __init__(self, world: dict):
        self.w = copy.deepcopy(world)

    def workloads(self, ns):
        return self.w["workloads"]

    def replicasets(self, ns):
        return self.w["replicasets"]

    def pods(self, ns):
        return self.w["pods"]

    def events(self, ns):
        return self.w["events"]

    def node_events(self):
        return []

    def logs(self, ns, pod, container, since_s=None, previous=False, tail=2000):
        return self.w["logs"].get((pod, previous), [])

    def services(self, ns=None):
        return self.w["services"]

    def endpoints(self, ns, name):
        n = self.w["endpoints"].get(name)
        if n is None:
            return {"exists": False, "ready": [], "not_ready": []}
        pods = [p["name"] for p in self.w["pods"] if p["app"] == name]
        return {"exists": True, "ready": [{"ip": "10.0.0.1", "pod": (pods or [None])[0]}] * n, "not_ready": []}

    def configmaps(self, ns):
        return self.w["configmaps"]

    def secret_values(self, ns, name):
        return self.w["secrets"].get(name, {})

    def probe_tcp(self, ns, pod, container, host, port, timeout=20):
        return {"host": host, "port": port, **self.w["probes"].get(host, {"dns": "error", "dns_error": "not known"})}

    def service_proxy_get(self, ns, service, port, path, timeout=10):
        return self.w["entry"]
