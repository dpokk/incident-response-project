"""Evidence collection: the common investigation process, identical for every incident.

    identify workloads -> Kubernetes state -> events -> logs -> services/endpoints ->
    configuration -> dependencies -> change history -> (optional) metrics

It records *facts* in an EvidenceStore and draws no conclusions; diagnosis.py interprets them.
It is not told what failure was injected: it inspects every workload in the namespace.
"""
from collections import defaultdict

from . import logparse
from .dependencies import check_dependency, extract_references
from .evidence import EvidenceStore
from .kube import read_journal
from .tools import Toolset

BAD_WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError",
               "CreateContainerError", "InvalidImageName", "RunContainerError"}


def hms(t) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(t).astimezone().strftime("%H:%M:%S") if t else "?"


def mib(b) -> str:
    return f"{b / 2**20:.0f}Mi" if b else "n/a"


def collect(tools: Toolset, incident: dict, start: float, end: float, journal_path=None, prom=None,
            entry: tuple | None = None, prom_entry: str = "frontend", log=print) -> EvidenceStore:
    store = tools.store
    ns = tools.ns
    for sig in incident.get("signals", []):
        store.add("detector", sig.get("subject", "system"), "detection_signal", sig["text"], t=sig.get("t"),
                  signal=sig.get("kind"))

    log("  [1/8] identifying workloads")
    store.step("identify_workloads", f"all workloads in namespace {ns}")
    workloads = tools.get_workloads() or []
    rs_owner = {rs["name"]: rs["deployment"] for rs in tools.get_replicasets() or []}
    pod_workload = {}
    for w in workloads:
        for p in tools.pods_of(w):
            pod_workload[p["name"]] = w["name"]

    log("  [2/8] Kubernetes state (workloads, pods, containers)")
    store.step("kubernetes_state", "workload replicas, pod phase/readiness, container state, termination history")
    journal = read_journal(journal_path, start - 60, end + 60) if journal_path else []
    for w in workloads:
        _workload_state(store, tools, w, journal, start, end)

    log("  [3/8] Kubernetes events")
    store.step("kubernetes_events", "events for pods, replica sets and workloads in the window; node events")
    _events(store, tools, pod_workload, rs_owner, start)

    log("  [4/8] application logs (current and previous container instances)")
    store.step("logs", "current logs since window start; previous-instance logs for restarted containers")
    for w in workloads:
        _logs(store, tools, w, start, end)

    log("  [5/8] services and endpoints")
    store.step("services", "services in the namespace and their ready endpoints")
    for s in tools.get_services() or []:
        ep = tools.get_endpoints(s["name"]) or {"ready": [], "not_ready": []}
        store.add("kubernetes.endpoints", f"service/{s['name']}", "service_state",
                  f"Service {ns}/{s['name']} (ports {[p['port'] for p in s['ports']]}): {len(ep['ready'])} ready, "
                  f"{len(ep['not_ready'])} not-ready endpoints",
                  service=s["name"], ready=len(ep["ready"]), not_ready=len(ep["not_ready"]), selector=s["selector"])

    log("  [6/8] configuration and dependencies")
    store.step("configuration", "effective container environment (ConfigMaps, Secrets masked) -> dependency references")
    for w in workloads:
        config = tools.get_configuration(w)
        refs = extract_references(config)
        store.step("dependencies", f"{w['name']}: {len(refs)} configured dependencies",
                   refs=[f"{r['host']}:{r['port']}" for r in refs])
        for ref in refs:
            check_dependency(tools, store, w, ref, tools.pods_of(w), start)

    log("  [7/8] change history (rollouts)")
    store.step("changes", "ReplicaSet revisions created shortly before/during the incident")
    for rs in tools.get_replicasets() or []:
        if rs["created"] and start - 1800 <= rs["created"] <= end and rs["revision"] not in (None, "1"):
            store.add("kubernetes.replicasets", f"workload/{rs['deployment']}", "rollout",
                      f"Deployment {rs['deployment']} rolled out revision {rs['revision']} (ReplicaSet {rs['name']})",
                      t=rs["created"], workload=rs["deployment"], revision=rs["revision"])

    if entry:
        store.step("entry_probe", f"synthetic request to {entry[0]}:{entry[1]}{entry[2]}")
        res = tools.probe_entry(*entry) or {}
        store.add("synthetic_probe", f"service/{entry[0]}", "entry_probe",
                  f"Synthetic request GET {entry[2]} via service {entry[0]} returned HTTP {res.get('status')}: "
                  f"{(res.get('body') or '').strip()[:120]}", status=res.get("status"), body=res.get("body"))

    if prom is not None:
        log("  [8/8] metrics (optional Prometheus enrichment)")
        store.step("metrics", "request rate, error ratio and memory from Prometheus")
        from .metrics import metric_facts
        try:
            metric_facts(store, prom, ns, incident, start, end, workloads, tools.get_pods() or [], prom_entry)
        except Exception as exc:  # noqa: BLE001 - metrics are optional
            store.step("metrics", f"skipped: {exc}")
    else:
        store.step("metrics", "skipped: no metrics source configured")
    return store


def _workload_state(store: EvidenceStore, tools: Toolset, w: dict, journal: list, start: float, end: float) -> None:
    subj = f"workload/{w['name']}"
    pods = tools.pods_of(w)
    store.add("kubernetes.workloads", subj, "workload_status",
              f"{w['kind']} {w['namespace']}/{w['name']}: {w['replicas_ready']}/{w['replicas_desired']} replicas ready",
              workload=w["name"], desired=w["replicas_desired"], ready=w["replicas_ready"],
              available=w["replicas_available"], pods=[p["name"] for p in pods],
              limits={c["name"]: c["limits"] for c in w["containers"]})
    seen_terms = set()
    for p in pods:
        restarts = sum(c["restart_count"] for c in p["containers"])
        store.add("kubernetes.pod_status", subj, "pod_status",
                  f"Pod {p['name']}: phase={p['phase']}, ready={p['ready']}, restarts={restarts}",
                  pod=p["name"], phase=p["phase"], ready=p["ready"], restarts=restarts, unschedulable=p["unschedulable"],
                  ready_since=p["ready_since"])
        for c in p["containers"]:
            st = c["state"] or {}
            if st.get("state") == "waiting" and st.get("reason"):
                store.add("kubernetes.pod_status", subj, "container_waiting",
                          f"Container {c['name']} in pod {p['name']} is waiting: {st['reason']}"
                          + (f" ({(st.get('message') or '')[:160]})" if st.get("message") else ""),
                          pod=p["name"], container=c["name"], reason=st["reason"], message=st.get("message"),
                          problematic=st["reason"] in BAD_WAITING)
            ls = c["last_state"] or {}
            if ls.get("state") == "terminated" and (ls.get("finished_at") or 0) >= start - 60:
                seen_terms.add((p["name"], c["name"], round(ls["finished_at"] or 0)))
                _termination(store, subj, p["name"], c, ls, restarts=c["restart_count"])
    for e in journal:  # earlier terminations of the same containers (kubelet only keeps the latest)
        if e["kind"] == "container_terminated" and e.get("app") and e["pod"] in {p["name"] for p in pods}:
            key = (e["pod"], e.get("container"), round(e["t"]))
            if any(k[0] == key[0] and k[1] == key[1] and abs(k[2] - key[2]) <= 2 for k in seen_terms):
                continue
            seen_terms.add(key)
            c = next((c for p in pods if p["name"] == e["pod"] for c in p["containers"] if c["name"] == e.get("container")), {})
            _termination(store, subj, e["pod"], {**c, "name": e.get("container")},
                         {"reason": e.get("reason"), "exit_code": e.get("exit_code"), "started_at": e.get("started_at"),
                          "finished_at": e["t"]}, restarts=e.get("restart_count"), source="kubernetes.pod_journal")


def _termination(store, subj, pod, c, ls, restarts=None, source="kubernetes.pod_status"):
    ran = (ls["finished_at"] - ls["started_at"]) if ls.get("finished_at") and ls.get("started_at") else None
    mem = c.get("memory_limit_bytes")
    store.add(source, subj, "container_terminated",
              f"Container {c.get('name')} in pod {pod} terminated: reason={ls.get('reason')}, exit code "
              f"{ls.get('exit_code')}" + (f", after running {ran:.0f}s" if ran is not None else "")
              + (f" (memory limit {mib(mem)})" if mem else ""),
              t=ls.get("finished_at"), pod=pod, container=c.get("name"), reason=ls.get("reason"),
              exit_code=ls.get("exit_code"), ran_s=ran, memory_limit=mem, restarts=restarts)


def _events(store: EvidenceStore, tools: Toolset, pod_workload: dict, rs_owner: dict, start: float) -> None:
    groups: dict[tuple, dict] = {}
    for e in tools.get_events() or []:
        if (e["last"] or e["first"] or 0) < start:
            continue
        obj = e["object_name"]
        workload = pod_workload.get(obj) or rs_owner.get(obj) or (obj if e["object_kind"] in ("Deployment", "StatefulSet") else None)
        if workload is None:  # pods that no longer exist: attribute by ReplicaSet/StatefulSet name prefix
            workload = next((w for w in set(rs_owner.values()) | set(pod_workload.values()) if w and obj.startswith(w + "-")), obj)
        key = (workload, e["object_kind"], e["reason"], logparse.normalize(e["message"].split(":")[0] if e["reason"] == "Unhealthy" else e["message"]))
        g = groups.setdefault(key, {"workload": workload, "kind": e["object_kind"], "reason": e["reason"],
                                    "type": e["type"], "message": e["message"][:300], "count": 0,
                                    "first": e["first"], "last": e["last"], "objects": set()})
        g["count"] += e["count"]
        g["first"] = min(filter(None, [g["first"], e["first"]]), default=None)
        g["last"] = max(filter(None, [g["last"], e["last"]]), default=None)
        g["objects"].add(obj)
    for g in sorted(groups.values(), key=lambda g: g["first"] or 0):
        store.add("kubernetes.events", f"workload/{g['workload']}", "k8s_event",
                  f"{g['type']} event {g['reason']} on {g['kind']} {', '.join(sorted(g['objects']))[:120]} "
                  f"(x{g['count']}, {hms(max(g['first'] or start, start))}-{hms(g['last'])}): {g['message'][:200]}",
                  t=max(g["first"] or start, start), reason=g["reason"], type=g["type"], count=g["count"],
                  object_kind=g["kind"], message=g["message"], last=g["last"])
    for e in tools.get_node_events() or []:
        if (e["last"] or e["first"] or 0) >= start and e["type"] == "Warning":
            store.add("kubernetes.events", f"node/{e['object_name']}", "node_event",
                      f"Node {e['object_name']}: {e['reason']}: {e['message'][:200]}", t=e["first"], reason=e["reason"])


def _logs(store: EvidenceStore, tools: Toolset, w: dict, start: float, end: float) -> None:
    subj = f"workload/{w['name']}"
    since = int(max(1, end - start)) + 30
    sigs: dict[tuple, dict] = {}
    levels = defaultdict(int)
    errors: dict[str, dict] = {}
    for p in tools.pods_of(w):
        for c in p["containers"]:
            instances = [("current", tools.get_logs(p["name"], c["name"], since_s=since) or [])]
            if c["restart_count"] > 0:
                instances.append(("previous", tools.get_logs(p["name"], c["name"], previous=True) or []))
            for instance, lines in instances:
                recs = [r for r in logparse.parse_records(lines) if r["_t"] is None or start - 5 <= r["_t"] <= end + 5]
                a = logparse.analyze(recs)
                for lvl, n in a["levels"].items():
                    levels[lvl] += n
                for s in a["signatures"]:
                    k = (s["signature"], s["target_host"], s["target_port"])
                    g = sigs.setdefault(k, {**s, "count": 0, "pods": set(), "instances": set()})
                    g["count"] += s["count"]
                    g["first"] = min(filter(None, [g["first"], s["first"]]), default=None)
                    g["last"] = max(filter(None, [g["last"], s["last"]]), default=None)
                    g["pods"].add(p["name"])
                    g["instances"].add(instance)
                for e in a["error_groups"]:
                    g = errors.setdefault(logparse.normalize(e["message"]), {**e, "count": 0})
                    g["count"] += e["count"]
                for tb in a["tracebacks"]:
                    site = tb["crash_site"] or {}
                    store.add("logs", subj, "log_exception",
                              f"{instance.capitalize()} container instance of {c['name']} in pod {p['name']} logged an "
                              f"unhandled {tb['type']}: {tb['message'][:160]}"
                              + (f" at {site.get('file')}:{site.get('line')} in {site.get('func')}()" if site else ""),
                              t=tb["t"], pod=p["name"], container=c["name"], instance=instance, exc_type=tb["type"],
                              message=tb["message"], crash_site=site, frames=tb["frames"],
                              dependency_signature=logparse.classify(tb["type"] + ": " + tb["message"]))
                if instance == "previous" and a["tail"]:
                    store.add("logs", subj, "log_tail_before_exit",
                              f"Last log lines of the previous {c['name']} instance in pod {p['name']}: "
                              + " | ".join(t[:120] for t in a["tail"][-3:]),
                              t=a["last"], pod=p["name"], container=c["name"], tail=a["tail"])
    for s in sorted(sigs.values(), key=lambda s: -s["count"]):
        target = f" referencing {s['target_host']}" + (f":{s['target_port']}" if s["target_port"] else "") if s["target_host"] else ""
        store.add("logs", subj, "log_signature",
                  f"{w['name']} logged {s['count']} {s['signature'].replace('_', ' ')} message(s){target} "
                  f"({hms(s['first'])}-{hms(s['last'])}), e.g. \"{s['sample'][:180]}\"",
                  t=s["first"], signature=s["signature"], target_host=s["target_host"], target_port=s["target_port"],
                  count=s["count"], pods=sorted(s["pods"]), instances=sorted(s["instances"]), last=s["last"],
                  sample=s["sample"])
    if levels.get("error") or levels.get("critical") or levels.get("warning"):
        top = sorted(errors.values(), key=lambda e: -e["count"])[:4]
        store.add("logs", subj, "log_levels",
                  f"{w['name']} logged {levels.get('error', 0) + levels.get('critical', 0)} error and "
                  f"{levels.get('warning', 0)} warning lines in the window"
                  + (f"; most frequent errors: " + "; ".join(f"\"{e['message'][:80]}\" x{e['count']}" for e in top) if top else ""),
                  errors=levels.get("error", 0) + levels.get("critical", 0), warnings=levels.get("warning", 0),
                  top_errors=[{"message": e["message"], "count": e["count"]} for e in top])
