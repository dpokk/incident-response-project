"""Investigation toolset: read-only functions over the running system.

These are the building blocks the investigation uses (and that a future AI agent could call as
tools). Every call is recorded in the evidence trace. Results are cached per investigation so
several collectors can ask the same question cheaply. No tool modifies cluster state.
"""
import time
from functools import wraps

from .evidence import EvidenceStore
from .kube import Kube


def _tool(fn):
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        key = (fn.__name__, args, tuple(sorted(kwargs.items())))
        if key in self._cache:
            return self._cache[key]
        t0 = time.time()
        try:
            result = fn(self, *args, **kwargs)
            status = "ok"
        except Exception as exc:  # noqa: BLE001 - a failed tool is recorded, not fatal
            result, status = None, f"error: {type(exc).__name__}: {str(exc)[:200]}"
        self.store.step("tool", fn.__name__, args=[str(a) for a in args],
                        kwargs={k: str(v) for k, v in kwargs.items()}, status=status,
                        ms=int((time.time() - t0) * 1000), summary=_summarize(result))
        self._cache[key] = result
        return result
    return wrapper


def _summarize(result) -> str:
    if result is None:
        return "none"
    if isinstance(result, list):
        return f"{len(result)} items"
    if isinstance(result, dict):
        return ", ".join(list(result)[:6])
    return str(result)[:80]


class Toolset:
    def __init__(self, kube: Kube, namespace: str, store: EvidenceStore, active_probes: bool = True):
        self.kube, self.ns, self.store, self.active_probes = kube, namespace, store, active_probes
        self._cache: dict = {}

    # -- workloads & pods -------------------------------------------------------
    @_tool
    def get_workloads(self) -> list[dict]:
        return self.kube.workloads(self.ns)

    @_tool
    def get_replicasets(self) -> list[dict]:
        return self.kube.replicasets(self.ns)

    @_tool
    def get_pods(self) -> list[dict]:
        return self.kube.pods(self.ns)

    def get_pod_status(self, pod: str) -> dict | None:
        return next((p for p in self.get_pods() or [] if p["name"] == pod), None)

    def get_container_status(self, pod: str, container: str) -> dict | None:
        p = self.get_pod_status(pod)
        return next((c for c in (p or {}).get("containers", []) if c["name"] == container), None)

    def pods_of(self, workload: dict) -> list[dict]:
        sel = workload.get("selector") or {}
        return [p for p in self.get_pods() or [] if sel and all(p["labels"].get(k) == v for k, v in sel.items())]

    # -- events & logs ------------------------------------------------------------
    @_tool
    def get_events(self) -> list[dict]:
        return self.kube.events(self.ns)

    def get_pod_events(self, pod: str) -> list[dict]:
        return [e for e in self.get_events() or [] if e["object_kind"] == "Pod" and e["object_name"] == pod]

    def get_object_events(self, kind: str, name_prefix: str) -> list[dict]:
        return [e for e in self.get_events() or [] if e["object_kind"] == kind and e["object_name"].startswith(name_prefix)]

    @_tool
    def get_node_events(self) -> list[dict]:
        return self.kube.node_events()

    @_tool
    def get_logs(self, pod: str, container: str, since_s: int | None = None, previous: bool = False) -> list:
        return self.kube.logs(self.ns, pod, container, since_s=since_s, previous=previous)

    # -- networking -----------------------------------------------------------------
    @_tool
    def get_services(self, all_namespaces: bool = False) -> list[dict]:
        return self.kube.services(None if all_namespaces else self.ns)

    @_tool
    def get_endpoints(self, service: str, namespace: str | None = None) -> dict:
        return self.kube.endpoints(namespace or self.ns, service)

    # -- configuration --------------------------------------------------------------
    @_tool
    def get_configmaps(self) -> list[dict]:
        return self.kube.configmaps(self.ns)

    @_tool
    def _secret(self, name: str) -> dict:
        return self.kube.secret_values(self.ns, name)

    def get_configuration(self, workload: dict) -> list[dict]:
        """Effective environment of each container, with where each value came from.

        Secret-sourced values are marked sensitive; they may be parsed (e.g. for a hostname) but are
        never written into facts or reports.
        """
        cms = {c["name"]: c for c in self.get_configmaps() or []}
        out = []
        for c in workload["containers"]:
            for ef in c["env_from"]:
                if "configmap" in ef:
                    cm = cms.get(ef["configmap"], {})
                    for k, v in cm.get("data", {}).items():
                        out.append({"container": c["name"], "name": k, "value": v, "sensitive": False,
                                    "source": f"configmap/{ef['configmap']}", "modified": cm.get("last_modified")})
                else:
                    for k, v in (self._secret(ef["secret"]) or {}).items():
                        out.append({"container": c["name"], "name": k, "value": v, "sensitive": True,
                                    "source": f"secret/{ef['secret']}", "modified": None})
            for e in c["env"]:
                src, val, sensitive, modified = "literal", e["value"], False, None
                if e["from"] and "configmap" in e["from"]:
                    cm = cms.get(e["from"]["configmap"], {})
                    val, src, modified = cm.get("data", {}).get(e["from"]["key"]), f"configmap/{e['from']['configmap']}", cm.get("last_modified")
                elif e["from"] and "secret" in e["from"]:
                    val = (self._secret(e["from"]["secret"]) or {}).get(e["from"]["key"])
                    src, sensitive = f"secret/{e['from']['secret']}", True
                elif e["from"] and "field" in e["from"]:
                    src, val = f"field/{e['from']['field']}", None
                out.append({"container": c["name"], "name": e["name"], "value": val, "sensitive": sensitive,
                            "source": src, "modified": modified})
        return out

    # -- active checks ------------------------------------------------------------------
    @_tool
    def probe_connectivity(self, pod: str, container: str, host: str, port: int) -> dict:
        if not self.active_probes:
            return {"skipped": "active probes disabled"}
        return self.kube.probe_tcp(self.ns, pod, container, host, port)

    @_tool
    def probe_entry(self, service: str, port: str, path: str) -> dict:
        status, body = self.kube.service_proxy_get(self.ns, service, port, path)
        return {"status": status, "body": body}
