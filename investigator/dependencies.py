"""Dependency checking: records observations about one configured dependency. Provider-independent.

Dependencies are discovered from each component's configuration (capability `get_dependencies`),
never from a hard-coded list. For each one this records: the configuration reference, a recent change
to the configuration that supplies it, what serves the endpoint (`get_service_health`), and whether a
running instance of the consumer can actually reach it (`check_connectivity`).
"""
from .capabilities import Capabilities
from .capabilities.base import ConnectivityResult, DependencyRef
from .evidence import EvidenceStore

CONFIG_CHANGE_LOOKBACK_S = 1800


def check_dependency(caps: Capabilities, store: EvidenceStore, consumer: str, ref: DependencyRef,
                     window_start: float, probe: bool = True) -> dict:
    """Collect facts about one configured dependency. Returns a small summary used for planning."""
    src = caps.resources.name
    subject = f"dependency/{ref.host}:{ref.port}"
    shown_source = ref.source + (" (secret, value not shown)" if ref.sensitive_source else "")
    store.add("configuration", f"workload/{consumer}", "config_reference",
              f"{consumer} is configured (env {ref.variable} from {shown_source}) to use "
              f"{ref.type} at {ref.host}:{ref.port}",
              consumer=consumer, host=ref.host, port=ref.port, dep_type=ref.type,
              variable=ref.variable, config_source=ref.source)
    if ref.source_modified and ref.source_modified >= window_start - CONFIG_CHANGE_LOOKBACK_S \
            and ref.source.startswith("configmap/"):
        store.add("configuration", ref.source, "config_changed",
                  f"{ref.source} (which supplies {ref.variable} to {consumer}) was last modified at this time",
                  t=ref.source_modified, consumer=consumer, variable=ref.variable, config_source=ref.source)

    h = caps.get_service_health(ref.host, ref.port, ref.type)
    summary = {"ref": ref, "subject": subject, "exists": None, "backing": [], "ready": None}
    if h is None:
        pass
    elif not h.internal:
        store.add("dependency_check", subject, "external_endpoint",
                  f"{ref.host} is outside the cluster; only connectivity can be checked", host=ref.host)
    elif not h.exists:
        summary["exists"] = False
        store.add(f"{src}.services", subject, "service_lookup",
                  f"No {h.kind} named '{h.name}' exists in {h.scope_kind} {h.scope}"
                  + (f" (found in: {', '.join(h.other_scopes)})" if h.other_scopes else f" or in any other {h.scope_kind}"),
                  host=ref.host, found=False, service=h.name, namespace=h.scope)
        for s in h.similar:
            store.add(f"{src}.services", subject, "similar_service",
                      f"{h.kind} {h.scope}/{s.name} exists with port(s) {s.ports} and {s.ready} ready endpoint(s)"
                      f" (name similarity {s.name_similarity:.0%} to '{h.name}')",
                      service=s.name, ports=s.ports, ready=s.ready, name_similarity=s.name_similarity,
                      port_match=s.port_match)
    else:
        summary.update(exists=True, ready=h.ready_endpoints, backing=[b.component for b in h.backing])
        store.add(f"{src}.services", subject, "service_lookup",
                  f"{h.kind} {h.scope}/{h.name} exists ({h.address_kind} {h.address}, ports {h.ports})",
                  host=ref.host, found=True, service=h.name, namespace=h.scope, ports=h.ports)
        if ref.port and ref.port not in h.ports:
            store.add(f"{src}.services", subject, "service_port_mismatch",
                      f"{h.kind} {h.scope}/{h.name} does not expose port {ref.port} (exposes {h.ports})",
                      configured_port=ref.port, service_ports=h.ports)
        store.add(f"{src}.endpoints", subject, "service_endpoints",
                  f"{h.kind} {h.scope}/{h.name} has {h.ready_endpoints} ready and {h.not_ready_endpoints} "
                  f"not-ready endpoints" + (f" ({h.instance_kind}s: {', '.join(h.endpoint_instances)})"
                                            if h.endpoint_instances else ""),
                  ready=h.ready_endpoints, not_ready=h.not_ready_endpoints, service=h.name)
        for b in h.backing:
            store.add(f"{src}.workloads", subject, "backing_workload",
                      f"{h.kind} {h.name} is backed by {b.kind} {b.component}: {b.ready}/{b.desired} replicas ready "
                      f"(desired {b.desired})", workload=b.component, desired=b.desired, ready=b.ready)

    if probe and ref.port:
        probe_dependency(caps, store, consumer, ref)
    return summary


def probe_dependency(caps: Capabilities, store: EvidenceStore, consumer: str, ref: DependencyRef) -> None:
    """Active check from a running consumer instance, plus any healthy alternative if the name doesn't exist."""
    subject = f"dependency/{ref.host}:{ref.port}"
    res = caps.check_connectivity(consumer, ref.host, int(ref.port))
    if res is None or res.from_instance is None:
        store.add("dependency_check", subject, "connectivity_probe_skipped",
                  f"No running {consumer} container to probe {ref.host}:{ref.port} from", host=ref.host)
        return
    _probe_fact(store, subject, res)
    for alt in store.find(kind="similar_service", subject=subject):
        if alt.data["port_match"] and alt.data["ready"] > 0:
            alt_res = caps.check_connectivity(consumer, alt.data["service"], int(ref.port))
            if alt_res is not None and alt_res.from_instance is not None:
                f = _probe_fact(store, f"dependency/{alt.data['service']}:{ref.port}", alt_res)
                f.data["alternative_for"] = ref.host


def _probe_fact(store: EvidenceStore, subject: str, r: ConnectivityResult):
    host, port, pod = r.host, r.port, r.from_instance
    if r.skipped:
        return store.add("dependency_probe", subject, "connectivity_probe_skipped",
                         f"Connectivity probe to {host}:{port} not performed ({r.skipped})", host=host)
    if r.dns != "ok":
        return store.add("dependency_probe", subject, "connectivity_probe",
                         f"From pod {pod}: DNS lookup of '{host}' failed ({r.error})",
                         from_pod=pod, host=host, port=port, dns="error", tcp=None, error=r.error)
    detail = {"ok": f"TCP connection to {host}:{port} succeeded in {r.ms} ms",
              "refused": f"TCP connection to {host}:{port} was refused",
              "timeout": f"TCP connection to {host}:{port} timed out"}.get(
        r.tcp, f"TCP connection to {host}:{port} failed ({r.error})")
    return store.add("dependency_probe", subject, "connectivity_probe",
                     f"From pod {pod}: '{host}' resolves to {', '.join(r.addresses or [])}; {detail}",
                     from_pod=pod, host=host, port=port, dns="ok", tcp=r.tcp, addresses=r.addresses, error=r.error)
