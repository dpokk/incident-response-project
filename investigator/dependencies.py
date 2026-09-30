"""Dependency discovery and checking.

Dependencies are discovered from each workload's *configuration* (URLs and HOST/PORT pairs in its
environment), never from a hard-coded list. `check_dependency` then gathers observations about the
configured endpoint: does a Service with that name exist, does it expose that port, does it have
ready endpoints, what workload backs it and what happened to that workload recently, and can a
consumer pod actually resolve and connect to it.
"""
import difflib
import re

from .evidence import EvidenceStore
from .tools import Toolset

_URL = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*)://(?:[^@/\s]+@)?(?P<host>[A-Za-z0-9.\-]+)(?::(?P<port>\d+))?")
SCHEME_TYPES = {"postgres": "PostgreSQL", "postgresql": "PostgreSQL", "mysql": "MySQL", "redis": "Redis",
                "mongodb": "MongoDB", "amqp": "RabbitMQ", "http": "HTTP service", "https": "HTTP service",
                "kafka": "Kafka"}
PORT_TYPES = {5432: "PostgreSQL", 3306: "MySQL", 6379: "Redis", 27017: "MongoDB", 5672: "RabbitMQ", 9092: "Kafka"}
SCHEME_PORTS = {"postgres": 5432, "postgresql": 5432, "mysql": 3306, "redis": 6379, "mongodb": 27017,
                "amqp": 5672, "http": 80, "https": 443}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def extract_references(config: list[dict]) -> list[dict]:
    """Find endpoints a workload is configured to talk to (URL values and *_HOST/*_PORT pairs)."""
    refs, by_name = [], {}
    for e in config:
        by_name[(e["container"], e["name"])] = e
    for e in config:
        val = e.get("value")
        if not isinstance(val, str):
            continue
        m = _URL.match(val.strip())
        if m and m.group("host") not in LOCAL_HOSTS:
            scheme = m.group("scheme").lower()
            port = int(m.group("port") or SCHEME_PORTS.get(scheme, 0)) or None
            refs.append(_ref(e, m.group("host"), port, SCHEME_TYPES.get(scheme) or PORT_TYPES.get(port or 0, "service")))
        elif e["name"].upper().endswith("_HOST") and val and val not in LOCAL_HOSTS and re.match(r"^[A-Za-z0-9.\-]+$", val):
            port_entry = by_name.get((e["container"], e["name"][:-5] + "_PORT"))
            port = int(port_entry["value"]) if port_entry and str(port_entry.get("value", "")).isdigit() else None
            refs.append(_ref(e, val, port, PORT_TYPES.get(port or 0, "service")))
    uniq = {}
    for r in refs:
        uniq.setdefault((r["host"], r["port"]), r)
    return list(uniq.values())


def _ref(entry: dict, host: str, port: int | None, dep_type: str) -> dict:
    return {"host": host, "port": port, "type": dep_type, "variable": entry["name"], "container": entry["container"],
            "source": entry["source"], "sensitive_source": entry["sensitive"], "source_modified": entry.get("modified")}


def split_host(host: str, default_ns: str) -> tuple[str, str | None, bool]:
    """Map a hostname to (service name, namespace, looks_in_cluster)."""
    parts = host.split(".")
    if len(parts) == 1:
        return parts[0], default_ns, True
    if len(parts) >= 3 and parts[2] == "svc":
        return parts[0], parts[1], True
    if len(parts) == 2:
        return parts[0], parts[1], True  # "name.namespace"
    return host, None, False  # external FQDN


def check_dependency(tools: Toolset, store: EvidenceStore, consumer: dict, ref: dict, consumer_pods: list[dict],
                     window_start: float) -> dict:
    """Collect facts about one configured dependency. Returns a small summary used for graph building."""
    subject = f"dependency/{ref['host']}:{ref['port']}"
    shown_source = ref["source"] + (" (secret, value not shown)" if ref["sensitive_source"] else "")
    store.add("configuration", f"workload/{consumer['name']}", "config_reference",
              f"{consumer['name']} is configured (env {ref['variable']} from {shown_source}) to use "
              f"{ref['type']} at {ref['host']}:{ref['port']}",
              consumer=consumer["name"], host=ref["host"], port=ref["port"], dep_type=ref["type"],
              variable=ref["variable"], config_source=ref["source"])
    if ref["source_modified"] and ref["source_modified"] >= window_start - 1800 and ref["source"].startswith("configmap/"):
        store.add("configuration", ref["source"], "config_changed",
                  f"{ref['source']} (which supplies {ref['variable']} to {consumer['name']}) was last modified at this time",
                  t=ref["source_modified"], consumer=consumer["name"], variable=ref["variable"], config_source=ref["source"])

    name, ns, in_cluster = split_host(ref["host"], tools.ns)
    summary = {"ref": ref, "subject": subject, "service": None, "backing": [], "exists": None}
    if not in_cluster:
        store.add("dependency_check", subject, "external_endpoint",
                  f"{ref['host']} is outside the cluster; only connectivity can be checked", host=ref["host"])
    else:
        services = tools.get_services(all_namespaces=True) or []
        svc = next((s for s in services if s["name"] == name and s["namespace"] == ns), None)
        summary["exists"] = svc is not None
        if svc is None:
            others = [s for s in services if s["name"] == name]
            store.add("kubernetes.services", subject, "service_lookup",
                      f"No Service named '{name}' exists in namespace {ns}"
                      + (f" (found in: {', '.join(s['namespace'] for s in others)})" if others else
                         " or in any other namespace"),
                      host=ref["host"], found=False, service=name, namespace=ns)
            _similar_services(tools, store, subject, ref, services, name, ns)
        else:
            summary["service"] = svc
            ports = [p["port"] for p in svc["ports"]]
            store.add("kubernetes.services", subject, "service_lookup",
                      f"Service {ns}/{name} exists (ClusterIP {svc['cluster_ip']}, ports {ports})",
                      host=ref["host"], found=True, service=name, namespace=ns, ports=ports)
            if ref["port"] and ref["port"] not in ports:
                store.add("kubernetes.services", subject, "service_port_mismatch",
                          f"Service {ns}/{name} does not expose port {ref['port']} (exposes {ports})",
                          configured_port=ref["port"], service_ports=ports)
            summary["backing"] = _endpoints_and_backing(tools, store, subject, svc)

    # Active check from inside a consumer pod: can it resolve the name and open a TCP connection?
    runner = next((p for p in consumer_pods if p["phase"] == "Running"
                   and any((c["state"] or {}).get("state") == "running" for c in p["containers"])), None)
    if runner and ref["port"]:
        ctr = next(c["name"] for c in runner["containers"] if (c["state"] or {}).get("state") == "running")
        res = tools.probe_connectivity(runner["name"], ctr, ref["host"], int(ref["port"])) or {}
        _probe_fact(store, subject, runner["name"], ref, res)
        # If the configured name doesn't exist, check whether a plausible alternative is reachable instead.
        for alt in store.find(kind="similar_service", subject=subject):
            if alt.data["port_match"] and alt.data["ready"] > 0:
                alt_ref = {**ref, "host": alt.data["service"]}
                res = tools.probe_connectivity(runner["name"], ctr, alt_ref["host"], int(ref["port"])) or {}
                f = _probe_fact(store, f"dependency/{alt_ref['host']}:{ref['port']}", runner["name"], alt_ref, res)
                if f:
                    f.data["alternative_for"] = ref["host"]
    elif ref["port"]:
        store.add("dependency_check", subject, "connectivity_probe_skipped",
                  f"No running {consumer['name']} container to probe {ref['host']}:{ref['port']} from", host=ref["host"])
    return summary


def _probe_fact(store, subject, pod, ref, res):
    host, port = ref["host"], ref["port"]
    if res.get("skipped") or res.get("error"):
        return store.add("dependency_probe", subject, "connectivity_probe_skipped",
                         f"Connectivity probe to {host}:{port} not performed ({res.get('skipped') or res.get('error')})",
                         host=host)
    if res.get("dns") != "ok":
        return store.add("dependency_probe", subject, "connectivity_probe",
                         f"From pod {pod}: DNS lookup of '{host}' failed ({res.get('dns_error')})",
                         from_pod=pod, host=host, port=port, dns="error", tcp=None, error=res.get("dns_error"))
    tcp = res.get("tcp")
    detail = {"ok": f"TCP connection to {host}:{port} succeeded in {res.get('tcp_ms')} ms",
              "refused": f"TCP connection to {host}:{port} was refused",
              "timeout": f"TCP connection to {host}:{port} timed out"}.get(
        tcp, f"TCP connection to {host}:{port} failed ({res.get('tcp_error')})")
    return store.add("dependency_probe", subject, "connectivity_probe",
                     f"From pod {pod}: '{host}' resolves to {', '.join(res.get('addresses', []))}; {detail}",
                     from_pod=pod, host=host, port=port, dns="ok", tcp=tcp, addresses=res.get("addresses"),
                     error=res.get("tcp_error"))


def _endpoints_and_backing(tools, store, subject, svc) -> list[dict]:
    ep = tools.get_endpoints(svc["name"], svc["namespace"]) or {"ready": [], "not_ready": []}
    store.add("kubernetes.endpoints", subject, "service_endpoints",
              f"Service {svc['namespace']}/{svc['name']} has {len(ep['ready'])} ready and {len(ep['not_ready'])} "
              f"not-ready endpoints" + (f" (pods: {', '.join(a['pod'] or a['ip'] for a in ep['ready'])})" if ep["ready"] else ""),
              ready=len(ep["ready"]), not_ready=len(ep["not_ready"]), service=svc["name"])
    backing = []
    if svc["namespace"] != tools.ns or not svc["selector"]:
        return backing
    for w in tools.get_workloads() or []:
        tmpl_labels = w["selector"]
        if tmpl_labels and all(tmpl_labels.get(k) == v for k, v in svc["selector"].items()):
            backing.append(w)
            store.add("kubernetes.workloads", subject, "backing_workload",
                      f"Service {svc['name']} is backed by {w['kind']} {w['name']}: {w['replicas_ready']}/"
                      f"{w['replicas_desired']} replicas ready (desired {w['replicas_desired']})",
                      workload=w["name"], desired=w["replicas_desired"], ready=w["replicas_ready"])
    return backing


def _similar_services(tools, store, subject, ref, services, name, ns):
    """Services that could be what the configuration meant: same port or dependency type, or a similar name."""
    for s in services:
        if s["namespace"] != ns:
            continue
        ports = [p["port"] for p in s["ports"]]
        name_sim = difflib.SequenceMatcher(None, s["name"], name).ratio()
        type_match = ref["port"] in ports or any(PORT_TYPES.get(p) == ref["type"] for p in ports)
        if type_match or name_sim >= 0.6:
            ep = tools.get_endpoints(s["name"], s["namespace"]) or {"ready": []}
            store.add("kubernetes.services", subject, "similar_service",
                      f"Service {ns}/{s['name']} exists with port(s) {ports} and {len(ep['ready'])} ready endpoint(s)"
                      f" (name similarity {name_sim:.0%} to '{name}')",
                      service=s["name"], ports=ports, ready=len(ep["ready"]), name_similarity=round(name_sim, 2),
                      port_match=ref["port"] in ports)
