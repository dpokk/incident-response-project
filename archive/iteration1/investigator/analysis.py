"""Deterministic analysis: turns raw evidence into signals, a timeline, scored hypotheses and impact.

Everything here is derived from the collected evidence bundle and every derived fact carries
evidence IDs, so the final report is traceable back to a Prometheus query, a Kubernetes
object/event or a log line. The LLM layer (llm.py) reasons over this output; it never sees
"something is wrong, figure it out".
"""
from collections import defaultdict
from statistics import median

# --------------------------------------------------------------------------- helpers

Points = list[tuple[float, float]]


def single(metrics: dict, name: str) -> Points:
    series = metrics.get(name, {}).get("series") or []
    return series[0]["values"] if series else []


def by_label(metrics: dict, name: str, *labels: str) -> dict:
    out = {}
    for s in metrics.get(name, {}).get("series") or []:
        key = tuple(s["labels"].get(l) for l in labels)
        out[key if len(labels) > 1 else key[0]] = s["values"]
    return out


def first_sustained(points: Points, pred, after=None, before=None, n: int = 2):
    run, cand = 0, None
    for t, v in points:
        if after is not None and t < after:
            continue
        if before is not None and t > before:
            break
        if pred(v):
            if run == 0:
                cand = t
            run += 1
            if run >= n:
                return cand
        else:
            run = 0
    return cand if run > 0 else None


def max_point(points: Points, after=None, before=None):
    pts = [(t, v) for t, v in points if (after is None or t >= after) and (before is None or t <= before)]
    return max(pts, key=lambda p: p[1]) if pts else (None, None)


def integrate(points: Points, start: float, end: float) -> float:
    pts = [(t, v) for t, v in points if start <= t <= end]
    return sum((t2 - t1) * (v1 + v2) / 2 for (t1, v1), (t2, v2) in zip(pts, pts[1:]))


def pod_limits(containers: list[dict], cpu_key: str, mem_key: str) -> dict:
    """Pod-level limit = sum of container limits (None if any container is unlimited)."""
    cpus = [c.get(cpu_key) for c in containers]
    mems = [c.get(mem_key) for c in containers]
    return {"cpu": sum(cpus) if cpus and None not in cpus else None,
            "mem": sum(mems) if mems and None not in mems else None}


def pct(x) -> str:
    return f"{x * 100:.0f}%" if x is not None else "n/a"


def mib(b) -> str:
    return f"{b / 2**20:.0f}Mi" if b is not None else "n/a"


class Evidence:
    def __init__(self):
        self.items: list[dict] = []
        self._keys: dict[str, str] = {}

    def add(self, key: str, source: str, title: str, detail: str = "", ref: str = "") -> str:
        if key in self._keys:
            return self._keys[key]
        eid = f"E{len(self.items) + 1}"
        self._keys[key] = eid
        self.items.append({"id": eid, "source": source, "title": title, "detail": detail, "ref": ref})
        return eid


# --------------------------------------------------------------------------- analysis


def analyze(bundle: dict, settings) -> dict:
    ev = Evidence()
    m, k8s, journal = bundle["metrics"], bundle["k8s"], bundle["journal"]
    inc = bundle["incident"]
    w_start, w_end = bundle["window"]["start"], bundle["window"]["end"]
    detected = inc.get("detected_at") or w_end
    ns, entry = settings.namespace, settings.entry_app
    q = lambda name: m.get(name, {}).get("query", "")  # noqa: E731
    timeline: list[dict] = []

    def add_event(t, kind, text, evidence, **extra):
        if t is not None:
            timeline.append({"t": t, "kind": kind, "text": text, "evidence": [e for e in evidence if e], **extra})

    # ---- resource model: pods -> deployments, limits --------------------------------
    rs_to_dep = {rs["name"]: rs["deployment"] for rs in k8s["replicasets"]}
    deployments = {d["name"]: d for d in k8s["deployments"]}
    pod_dep: dict[str, str] = {}
    limits: dict[tuple, dict] = {}
    for p in k8s["pods"]:
        pod_dep[p["name"]] = rs_to_dep.get(p["owner"], p["app"])
        for c in p["containers"]:
            limits[(p["name"], c["name"])] = {"cpu": c["cpu_limit_cores"], "mem": c["memory_limit_bytes"]}
        limits[(p["name"], None)] = pod_limits(p["containers"], "cpu_limit_cores", "memory_limit_bytes")
    for e in journal:
        pod_dep.setdefault(e["pod"], e.get("app"))

    def dep_of(pod: str):
        if pod in pod_dep:
            return pod_dep[pod]
        return next((d for d in deployments if pod.startswith(d + "-")), None)

    def limit_of(pod, container, kind):
        """Limit for one container, or for the whole pod when container is None (pod-level metrics)."""
        lim = limits.get((pod, container), {}).get(kind)
        if lim is None:  # pod no longer exists: fall back to the deployment's pod template
            from .kube import parse_cpu, parse_mem
            d = deployments.get(dep_of(pod) or "")
            specs = [c for c in (d or {}).get("containers", []) if container is None or c["name"] == container]
            parsed = [{"cpu": parse_cpu(c["limits"].get("cpu")), "mem": parse_mem(c["limits"].get("memory"))} for c in specs]
            lim = pod_limits(parsed, "cpu", "mem")[kind] if parsed else None
        return lim

    # ---- traffic ---------------------------------------------------------------------
    rps = single(m, "entry_rps")
    e_rps = ev.add("m.rps", "prometheus", f"Request rate at '{entry}' (user-facing edge)",
                   "req/s over the incident window", q("entry_rps"))
    pre = [v for t, v in rps if t < detected - 20] if inc.get("detected_at") else []
    if len(pre) < 3:
        pre = [v for _, v in rps[: max(3, len(rps) // 4)]]
    baseline_rps = median(pre) if pre else None
    peak_t, peak_rps = max_point(rps)
    traffic = {"baseline_rps": baseline_rps, "peak_rps": peak_rps, "peak_t": peak_t,
               "spike": False, "onset_t": None, "return_t": None}
    if baseline_rps is not None and peak_rps and peak_rps >= max(baseline_rps * 1.5, baseline_rps + 10):
        traffic["spike"] = True
        mid = baseline_rps + 0.3 * (peak_rps - baseline_rps)
        cross = first_sustained(rps, lambda v: v >= mid, n=1)
        before = [p for p in rps if p[0] < cross]
        onset = cross
        for i in range(len(before) - 1, -1, -1):
            if before[i][1] <= baseline_rps * 1.15 + 1:
                onset = before[i + 1][0] if i + 1 < len(before) else cross
                break
        traffic["onset_t"] = onset
        traffic["multiplier"] = peak_rps / baseline_rps if baseline_rps else None
        reached = first_sustained(rps, lambda v: v >= 0.9 * peak_rps, after=onset, n=1)
        traffic["return_t"] = first_sustained(rps, lambda v: v <= baseline_rps * 1.3, after=peak_t, n=3)
        add_event(onset, "traffic", f"Request rate at {entry} began rising above baseline (~{baseline_rps:.0f} req/s)", [e_rps])
        add_event(reached, "traffic",
                  f"Request rate reached ~{peak_rps:.0f} req/s ({traffic['multiplier']:.1f}x baseline)", [e_rps])
        add_event(traffic["return_t"], "traffic", f"Request rate returned to baseline (~{baseline_rps:.0f} req/s)", [e_rps])

    # ---- errors & latency at the edge ------------------------------------------------
    ratio = single(m, "entry_error_ratio")
    err_rps = single(m, "entry_5xx_rps")
    e_err = ev.add("m.err", "prometheus", f"HTTP 5xx ratio at '{entry}'", "5xx responses / all responses",
                   q("entry_error_ratio"))
    thr = settings.error_rate_threshold
    err_start = first_sustained(ratio, lambda v: v >= thr, after=w_start, n=2)
    err_peak_t, err_peak = max_point(ratio)
    err_last = max((t for t, v in ratio if v >= thr), default=None)
    errors = {"start_t": err_start, "peak_ratio": err_peak, "peak_t": err_peak_t, "last_t": err_last,
              "baseline_ratio": median([v for t, v in ratio if t < (traffic["onset_t"] or detected) - 5] or [0])}
    by_code_series = by_label(m, "entry_5xx_by_code", "code")
    e_codes = ev.add("m.codes", "prometheus", f"5xx responses by status code at '{entry}'", "", q("entry_5xx_by_code"))
    if err_start:
        at_start = next((v for t, v in err_rps if t >= err_start), None)
        add_event(err_start, "errors", f"HTTP 5xx error rate at {entry} exceeded {pct(thr)}"
                  + (f" ({at_start:.0f} errors/s)" if at_start else ""), [e_err])
    if err_peak and err_peak >= thr:
        add_event(err_peak_t, "errors", f"HTTP 5xx error rate peaked at {pct(err_peak)}", [e_err, e_codes])

    p95 = single(m, "entry_p95_latency_s")
    e_lat = ev.add("m.p95", "prometheus", f"p95 latency at '{entry}'", "", q("entry_p95_latency_s"))
    base_p95 = median([v for t, v in p95 if t < (traffic["onset_t"] or detected) - 5] or [0.05])
    lat_start = first_sustained(p95, lambda v: v >= max(3 * base_p95, 0.25), after=traffic["onset_t"] or w_start, n=2)
    lat_peak_t, lat_peak = max_point(p95)
    latency = {"baseline_p95_s": base_p95, "degraded_t": lat_start, "peak_p95_s": lat_peak, "peak_t": lat_peak_t}
    if lat_start:
        add_event(lat_start, "latency",
                  f"p95 latency at {entry} degraded to {next(v for t, v in p95 if t >= lat_start) * 1000:.0f} ms "
                  f"(baseline ~{base_p95 * 1000:.0f} ms)", [e_lat])

    # ---- container terminations, restarts and readiness -----------------------------
    # ---- pod lifecycle: separate the incident from rollout churn and startup transients ------
    incident_ref = traffic["onset_t"] or err_start or lat_start or detected
    current_pods = {p["name"]: p for p in k8s["pods"]}
    deleted_at = {e["pod"]: e["t"] for e in journal if e["kind"] == "pod_deleted"}
    first_sample, last_sample = {}, {}
    # Liveness comes from the CPU counter: cAdvisor can keep exporting a deleted pod's leftover memory
    # cgroup gauge for minutes, but the CPU usage series ends when the pod's processes are gone.
    for name in ("cpu_cores_by_pod",):
        for s in m.get(name, {}).get("series") or []:
            pod = s["labels"].get("pod")
            if pod and s["values"]:
                first_sample[pod] = min(first_sample.get(pod, s["values"][0][0]), s["values"][0][0])
                last_sample[pod] = max(last_sample.get(pod, 0), s["values"][-1][0])

    def relevant(pod: str) -> bool:
        """False for pods that were already gone (e.g. replaced by a rollout) before the incident began."""
        if pod in deleted_at:
            return deleted_at[pod] >= incident_ref - 5
        if pod in current_pods:
            return True
        return last_sample.get(pod, 0) >= incident_ref - 5

    def settled(pod: str) -> float:
        """Ignore a pod's first 60s: interpreter start-up and warm-up spike CPU/memory briefly."""
        created = current_pods[pod]["created"] if pod in current_pods else first_sample.get(pod)
        return max(w_start, (created or w_start) + 60)

    terms: dict[tuple, dict] = {}
    for e in journal:
        if e["kind"] == "container_terminated":
            terms[(e["pod"], e.get("container"), round(e["t"]))] = {
                "pod": e["pod"], "container": e.get("container"), "t": e["t"], "reason": e.get("reason"),
                "exit_code": e.get("exit_code"), "source": "journal"}
    for p in k8s["pods"]:
        for c in p["containers"]:
            ls = c["last_state"] or {}
            if ls.get("state") == "terminated" and ls.get("finished_at") and w_start - 60 <= ls["finished_at"] <= w_end + 60:
                key = (p["name"], c["name"], round(ls["finished_at"]))
                if not any(k[0] == key[0] and k[1] == key[1] and abs(k[2] - key[2]) <= 2 for k in terms):
                    terms[key] = {"pod": p["name"], "container": c["name"], "t": ls["finished_at"],
                                  "reason": ls.get("reason"), "exit_code": ls.get("exit_code"), "source": "pod_status"}
    terminations = sorted((t for t in terms.values() if relevant(t["pod"])), key=lambda x: x["t"])

    # Only events on pods that took part in the incident and still occurring around it. Deployment-level
    # rollout events (ScalingReplicaSet etc.) are covered by the change detection below.
    events = [e for e in k8s["events"]
              if e["object_kind"] == "Pod" and relevant(e["object_name"])
              and (e["last"] or e["first"] or 0) >= incident_ref - 30]
    grouped_events: dict[tuple, dict] = {}
    for e in events:
        # Aggregated events can start long before (or during pod start-up, when probes are expected to fail)
        e = {**e, "first": max(e["first"] or w_start, w_start,
                               settled(e["object_name"]) if e["reason"] == "Unhealthy" else w_start)}
        if e["last"] and e["first"] > e["last"]:
            continue
        kind = e["message"].split(":")[0][:80] if e["reason"] == "Unhealthy" else e["reason"]
        key = (e["object_name"], e["reason"], kind)
        g = grouped_events.setdefault(key, {"object": e["object_name"], "reason": e["reason"], "type": e["type"],
                                            "message": e["message"][:300], "count": 0, "first": e["first"],
                                            "last": e["last"]})
        g["count"] += e["count"]
        g["first"] = min(filter(None, [g["first"], e["first"]]), default=None)
        g["last"] = max(filter(None, [g["last"], e["last"]]), default=None)
    event_groups = sorted(grouped_events.values(), key=lambda g: g["first"] or 0)

    # target workload = the deployment showing the strongest instability / saturation signals
    dep_score: dict[str, float] = defaultdict(float)
    for t in terminations:
        dep_score[dep_of(t["pod"])] += 3
    for g in event_groups:
        if g["type"] == "Warning":
            dep_score[dep_of(g["object"])] += 1
    for e in journal:
        if e["kind"] in ("pod_not_ready", "container_waiting"):
            dep_score[dep_of(e["pod"])] += 1

    # ---- CPU & memory per pod ----------------------------------------------------------
    cpu_series = by_label(m, "cpu_cores_by_pod", "pod", "container")
    thr_series = by_label(m, "cpu_throttle_ratio_by_pod", "pod", "container")
    mem_series = by_label(m, "memory_working_set_by_pod", "pod", "container")
    resource = defaultdict(dict)
    for (pod, ctr), pts in cpu_series.items():
        lim = limit_of(pod, ctr, "cpu")
        if not lim or not relevant(pod):
            continue
        r = [(t, v / lim) for t, v in pts if t >= settled(pod)]
        pt, pv = max_point(r)
        pre = [v for t, v in r if t < incident_ref]
        resource[(pod, ctr)].update(cpu_limit=lim, cpu_peak_ratio=pv, cpu_peak_t=pt,
                                    cpu_baseline_ratio=median(pre) if pre else None,
                                    cpu_80_t=first_sustained(r, lambda v: v >= 0.8, n=2))
        if pv and pv >= 0.8:
            dep_score[dep_of(pod)] += 1
    for (pod, ctr), pts in thr_series.items():
        if not relevant(pod):
            continue
        r = [(t, v) for t, v in pts if t >= settled(pod)]
        pt, pv = max_point(r)
        resource[(pod, ctr)].update(throttle_peak=pv, throttle_25_t=first_sustained(r, lambda v: v >= 0.25, n=2))
    for (pod, ctr), pts in mem_series.items():
        lim = limit_of(pod, ctr, "mem")
        if not lim or not relevant(pod):
            continue
        r = [(t, v / lim) for t, v in pts if t >= settled(pod)]
        pt, pv = max_point(r)
        pre = [(t, v) for t, v in r if t < incident_ref]
        resource[(pod, ctr)].update(mem_limit=lim, mem_peak_ratio=pv, mem_peak_t=pt,
                                    mem_baseline_ratio=median([v for _, v in pre]) if pre else None,
                                    mem_80_t=first_sustained(r, lambda v: v >= 0.8, n=1),
                                    # leak detection needs >= 2 minutes of settled pre-incident data
                                    mem_pre_trend=_trend(pre) if len(pre) >= 24 else None)
        if pv and pv >= 0.8:
            dep_score[dep_of(pod)] += 1

    dep_score.pop(None, None)
    target_dep = max(dep_score, key=lambda d: (dep_score[d], d != entry)) if dep_score else entry
    target = deployments.get(target_dep, {})
    target_ctrs = {c["name"] for c in target.get("containers", [])}
    target_pods = sorted(p for p in ({p for p, d in pod_dep.items() if d == target_dep} |
                                     {p for (p, _c) in resource if dep_of(p) == target_dep}) if relevant(p))
    target_service = next((s for s in k8s["services"]
                           if s["selector"] and all(target.get("selector", {}).get(k) == v for k, v in s["selector"].items())),
                          None)
    tres = {k: v for k, v in resource.items()
            if k[0] in target_pods and (k[1] is None or not target_ctrs or k[1] in target_ctrs)}

    e_cpu = ev.add("m.cpu", "prometheus", f"Container CPU usage vs limit ({target_dep} pods)",
                   "cAdvisor container_cpu_usage_seconds_total / CPU limit", q("cpu_cores_by_pod"))
    e_thr = ev.add("m.throttle", "prometheus", f"CPU CFS throttling ratio ({target_dep} pods)", "",
                   q("cpu_throttle_ratio_by_pod"))
    e_mem = ev.add("m.mem", "prometheus", f"Container memory working set vs limit ({target_dep} pods)",
                   "cAdvisor container_memory_working_set_bytes / memory limit", q("memory_working_set_by_pod"))

    cpu_hits = sorted((r["cpu_80_t"], pod) for (pod, _), r in tres.items() if r.get("cpu_80_t"))
    cpu_sat_t = cpu_hits[0][0] if cpu_hits else None
    if cpu_hits:
        r0 = next(r for (pod, _), r in tres.items() if pod == cpu_hits[0][1])
        base = [r["cpu_baseline_ratio"] for r in tres.values() if r.get("cpu_baseline_ratio") is not None]
        add_event(cpu_sat_t, "cpu",
                  f"{target_dep} CPU usage exceeded 80% of its {r0['cpu_limit'] * 1000:.0f}m limit "
                  f"({len(cpu_hits)}/{len(tres) or 1} pods; peak {pct(max(r.get('cpu_peak_ratio') or 0 for r in tres.values()))}"
                  + (f", baseline ~{pct(median(base))}" if base else "") + ")",
                  [e_cpu], pods=[p for _, p in cpu_hits])
    # Throttling alone is noisy for bursty runtimes; only report it alongside real CPU saturation.
    thr_hits = sorted(r["throttle_25_t"] for r in tres.values()
                      if r.get("throttle_25_t") and cpu_sat_t and r["throttle_25_t"] >= cpu_sat_t - 30)
    if thr_hits:
        add_event(thr_hits[0], "cpu", f"{target_dep} containers heavily CPU-throttled "
                  f"(peak {pct(max(r.get('throttle_peak') or 0 for r in tres.values()))} of CFS periods)", [e_thr])

    # memory: metrics plus the app's own cgroup-based stats log lines (finer grained)
    log_mem_hits = []
    for key, recs in bundle["logs"].items():
        pod = key.split("/")[0]
        if pod not in target_pods:
            continue
        for r in recs:
            if r.get("msg") == "stats" and (r.get("mem_limit_ratio") or 0) >= 0.8 and r.get("t"):
                log_mem_hits.append((r["t"], pod, r["mem_limit_ratio"]))
                break
    mem_hits = sorted((r["mem_80_t"], pod) for (pod, _), r in tres.items() if r.get("mem_80_t"))
    mem_peak = max((r.get("mem_peak_ratio") or 0 for r in tres.values()), default=0)
    mem_limit = next((r["mem_limit"] for r in tres.values() if r.get("mem_limit")), None)
    e_memlog = None
    if log_mem_hits:
        e_memlog = ev.add("log.memstats", "logs", f"{target_dep} application stats logs (cgroup memory usage)",
                          "periodic 'stats' log lines with mem_limit_ratio", f"kubectl logs -n {ns} <pod>")
    cand = sorted([(t, p, "metrics") for t, p in mem_hits] + [(t, p, "logs") for t, p, _ in log_mem_hits])
    mem_pressure_t = cand[0][0] if cand else None
    if cand:
        add_event(mem_pressure_t, "memory",
                  f"{target_dep} memory usage passed 80% of its {mib(mem_limit)} limit"
                  + (f" (peak observed {pct(max(mem_peak, max((h[2] for h in log_mem_hits), default=0)))})"),
                  [e_mem if mem_hits else None, e_memlog], pods=sorted({c[1] for c in cand}))

    # terminations (compress repeats: first per pod listed, remainder summarised)
    target_terms = [t for t in terminations if t["pod"] in target_pods]
    e_terms = {}
    oom_count = sum(1 for t in target_terms if t["reason"] == "OOMKilled")
    first_term_by_pod = {}
    for t in target_terms:
        src = "kubernetes.pod_journal" if t["source"] == "journal" else "kubernetes.pod_status"
        eid = ev.add(f"term.{t['pod']}", src, f"Container termination records for pod {t['pod']}",
                     "lastState.terminated (reason, exitCode, finishedAt)", f"pod/{t['pod']} -n {ns}")
        e_terms[t["pod"]] = eid
        if t["pod"] not in first_term_by_pod:
            first_term_by_pod[t["pod"]] = t
            add_event(t["t"], "termination",
                      f"Container {t['container']} in pod {t['pod']} terminated: {t['reason']} (exit code {t['exit_code']})"
                      + (f", memory limit {mib(limit_of(t['pod'], t['container'], 'mem'))}" if t["reason"] == "OOMKilled" else ""),
                      [eid], pods=[t["pod"]])
    if len(target_terms) > len(first_term_by_pod):
        last = target_terms[-1]
        add_event(last["t"], "termination",
                  f"{len(target_terms)} container terminations in total across {len(first_term_by_pod)} {target_dep} pods "
                  f"({oom_count} OOMKilled); last at this time", list(e_terms.values()))
    oom_metric = by_label(m, "oom_events_by_pod", "pod", "container")
    e_oom_metric = None
    if any(v > 0 for (p, _), pts in oom_metric.items() if p in target_pods for _, v in pts):
        e_oom_metric = ev.add("m.oom", "prometheus", "cAdvisor container OOM event counter", "", q("oom_events_by_pod"))

    # restarts (container starts after a termination)
    starts = sorted({(round(e["t"]), e["pod"]) for e in journal
                     if e["kind"] == "container_started" and e["pod"] in target_pods})
    pst = by_label(m, "process_start_time_by_pod", "pod")
    e_pst = ev.add("m.pst", "prometheus", f"Process start time of {target_dep} containers (restart detection)", "",
                   q("process_start_time_by_pod"))
    metric_starts = sorted({(round(v), pod) for pod, pts in pst.items() if pod in target_pods
                            for _, v in pts if w_start <= v <= w_end})
    all_starts = sorted(set(starts) | {s for s in metric_starts if not any(abs(s[0] - x[0]) <= 3 and s[1] == x[1] for x in starts)})
    first_restart = next(((t, p) for t, p in all_starts if first_term_by_pod.get(p) and t >= first_term_by_pod[p]["t"] - 1), None)
    if first_restart:
        add_event(first_restart[0], "restart", f"Kubernetes restarted the container in pod {first_restart[1]} "
                  f"({len([s for s in all_starts if s[0] >= first_restart[0] - 1])} container starts in window)",
                  [e_pst, e_terms.get(first_restart[1])], pods=[first_restart[1]])

    # Kubernetes events (warnings for the target and its pods)
    e_events = {}
    for g in event_groups:
        if dep_of(g["object"]) != target_dep and g["object"] != target_dep and not g["object"].startswith(target_dep):
            continue
        if g["type"] != "Warning" and g["reason"] not in ("Killing", "OOMKilling"):
            continue
        eid = ev.add(f"evt.{g['object']}.{g['reason']}.{g['message'][:30]}", "kubernetes.events",
                     f"{g['type']} event {g['reason']} on {g['object']} (x{g['count']})", g["message"],
                     f"kubectl get events -n {ns} --field-selector involvedObject.name={g['object']}")
        e_events.setdefault(g["reason"], []).append((g["first"], g, eid))
    for reason, items in e_events.items():
        items.sort(key=lambda x: x[0] or 0)
        t0, g0, _ = items[0]
        total = sum(g["count"] for _, g, _ in items)
        label = {"Unhealthy": "Health probe failures", "BackOff": "CrashLoopBackOff (back-off restarting failed container)",
                 "Killing": "Kubelet killing container"}.get(reason, reason)
        add_event(t0, "k8s_event", f"{label} began for {target_dep} pods: \"{g0['message'][:120]}\" "
                  f"({total} occurrences across {len({g['object'] for _, g, _ in items})} pods)",
                  [eid for _, _, eid in items])

    # ready-endpoint reconstruction from the pod journal
    e_journal = ev.add("journal", "kubernetes.pod_journal", "Pod readiness / restart transitions (watch stream)",
                       "recorded by the investigator's pod watcher", str(settings.state_dir / "pod_journal.jsonl"))
    ready = {p: True for p in target_pods if (current_pods.get(p, {}).get("created") or 0) < incident_ref}
    zero_start, zero_periods, min_ready = None, [], len(ready)
    last_ready_t = None
    for e in journal:
        if e["t"] < incident_ref - 30:  # rollout churn before the incident is handled by change detection
            continue
        if e["pod"] not in ready and e["kind"] != "pod_created":
            continue
        if e["kind"] == "pod_ready":
            ready[e["pod"]] = True
            last_ready_t = e["t"]
        elif e["kind"] in ("pod_not_ready", "container_terminated"):
            ready[e["pod"]] = False
        elif e["kind"] == "pod_deleted":
            ready.pop(e["pod"], None)
        elif e["kind"] == "pod_created" and dep_of(e["pod"]) == target_dep:
            ready[e["pod"]] = False
        n_ready = sum(ready.values())
        min_ready = min(min_ready, n_ready)
        if n_ready == 0 and zero_start is None:
            zero_start = e["t"]
        elif n_ready > 0 and zero_start is not None:
            zero_periods.append((zero_start, e["t"]))
            zero_start = None
    if zero_start is not None:
        zero_periods.append((zero_start, None))
    first_not_ready = next((e for e in journal if e["kind"] == "pod_not_ready" and e["pod"] in target_pods), None)
    if first_not_ready:
        add_event(first_not_ready["t"], "availability", f"Pod {first_not_ready['pod']} marked NotReady "
                  f"(removed from {target_service['name'] if target_service else target_dep} service endpoints)",
                  [e_journal], pods=[first_not_ready["pod"]])
    zero_total = sum(((b or w_end) - a) for a, b in zero_periods)
    if zero_periods:
        add_event(zero_periods[0][0], "availability",
                  f"No ready {target_dep} endpoints: all {len(target_pods)} pods unavailable "
                  f"({len(zero_periods)} period(s), {zero_total:.0f}s total)", [e_journal])

    # ---- recovery -------------------------------------------------------------------------
    last_bad = max([t["t"] for t in target_terms] + [err_last or 0] + [a for a, _ in zero_periods] + [0])
    ratio_ok_t = first_sustained(ratio, lambda v: v < 0.01, after=err_last, n=4) if err_last else None
    pods_now_ready = all(p["ready"] for p in k8s["pods"] if dep_of(p["name"]) == target_dep)
    all_ready_t = last_ready_t if pods_now_ready and last_ready_t and last_ready_t >= (target_terms[-1]["t"] if target_terms else 0) else None
    recovered_t = None
    if pods_now_ready and (ratio_ok_t or not err_start):
        recovered_t = max(filter(None, [ratio_ok_t, all_ready_t])) if (ratio_ok_t or all_ready_t) else None
    if all_ready_t and target_terms:
        add_event(all_ready_t, "recovery", f"All {target_dep} pods Ready again (service endpoints restored)", [e_journal])
    if ratio_ok_t:
        add_event(ratio_ok_t, "recovery", f"HTTP 5xx error rate at {entry} back below 1% (baseline)", [e_err])

    # ---- change & infrastructure checks (alternative explanations) -------------------------
    ref_t = traffic["onset_t"] or err_start or detected
    rollouts = [rs for rs in k8s["replicasets"] if rs["created"] and ref_t - 1800 <= rs["created"] <= ref_t
                and rs["revision"] not in (None, "1")]
    cm_changes = [c for c in k8s["configmaps"] if c["last_modified"] and ref_t - 1800 <= c["last_modified"] <= ref_t
                  and (c["last_modified"] - (c["created"] or 0)) > 5]
    e_changes = ev.add("changes", "kubernetes.replicasets+configmaps", "Deployment rollouts / ConfigMap edits before incident",
                       f"{len(rollouts)} rollouts, {len(cm_changes)} ConfigMap edits in the 30 min before onset",
                       f"kubectl get rs,cm -n {ns}")
    for rs in rollouts:
        add_event(rs["created"], "change", f"Rollout: ReplicaSet {rs['name']} (revision {rs['revision']}) created", [e_changes])
    for c in cm_changes:
        add_event(c["last_modified"], "change", f"ConfigMap {c['name']} modified", [e_changes])
    node_issues = [e for e in k8s["node_events"] if e["reason"] in
                   ("NodeNotReady", "SystemOOM", "EvictionThresholdMet", "NodeHasInsufficientMemory", "Rebooted")]
    evictions = [g for g in event_groups if g["reason"] in ("Evicted", "Preempted", "FailedScheduling")]
    e_node = ev.add("node", "kubernetes.events", "Node-level events", f"{len(node_issues)} node warnings, {len(evictions)} evictions",
                    "kubectl get events -A --field-selector involvedObject.kind=Node")

    # ---- detection marker ---------------------------------------------------------------
    e_det = ev.add("detector", "detector", "Incident detection signal",
                   ", ".join(inc.get("triggers", [])) or "manual investigation", "")
    if inc.get("detected_at"):
        add_event(inc["detected_at"], "detection", "Incident detected by monitor: " + "; ".join(inc.get("triggers", [])), [e_det])

    # ---- logs ---------------------------------------------------------------------------
    log_groups, last_lines = _summarize_logs(bundle["logs"], dep_of, target_pods, ev, ns)
    for g in log_groups:
        if g["deployment"] == target_dep and g["level"] in ("warning", "error"):
            add_event(g["first"], "log", f"{target_dep} logged {g['level'].upper()}: \"{g['msg']}\" "
                      f"(x{g['count']}, {len(g['pods'])} pods)", [g["evidence"]])
        elif g["deployment"] == entry and g["level"] == "error" and entry != target_dep:
            add_event(g["first"], "log", f"{entry} logged ERROR: \"{g['msg']}\" ({g['count']} failed requests"
                      + (f", reasons: {', '.join(sorted(g['reasons']))}" if g.get("reasons") else "") + ")", [g["evidence"]])

    # ---- hypotheses --------------------------------------------------------------------
    first_failure = min(filter(None, [err_start, target_terms[0]["t"] if target_terms else None,
                                      first_not_ready["t"] if first_not_ready else None]), default=None)
    saturation_t = min(filter(None, [cpu_sat_t, mem_pressure_t]), default=None)
    onset = traffic["onset_t"]
    pre_trend = max((r.get("mem_pre_trend") or 0 for r in tres.values()), default=0)
    last_change_t = max([rs["created"] for rs in rollouts] + [c["last_modified"] for c in cm_changes], default=None)
    # Did the service run cleanly after the change (once pods settled) before things went wrong?
    healthy_after_change = False
    if last_change_t and first_failure:
        calm = [v for t, v in ratio if last_change_t + 30 <= t < min(first_failure, onset or first_failure)]
        healthy_after_change = len(calm) >= 3 and max(calm) < thr and not any(
            last_change_t + 30 <= t["t"] < first_failure for t in target_terms)
    ev_trigger = [e_rps, e_cpu, e_mem, e_err] + list(e_terms.values())[:2]

    def H(hid, title, checks):
        total = sum(w for _, _, w, _ in checks)
        score = sum(w for _, ok, w, _ in checks if ok) / total if total else 0
        return {"id": hid, "title": title, "score": round(score, 3),
                "checks": [{"check": c, "result": bool(ok), "weight": w, "evidence": [e for e in eids if e]}
                           for c, ok, w, eids in checks]}

    hyps = [
        H("traffic_resource_exhaustion",
          f"Traffic spike exhausted {target_dep} CPU/memory limits, causing pod instability and request failures", [
              ("Request rate rose >=1.5x above baseline", traffic["spike"], 0.2, [e_rps]),
              ("Traffic increase preceded resource saturation", bool(onset and saturation_t and onset <= saturation_t + 5), 0.15, [e_rps, e_cpu, e_mem]),
              ("Resource saturation preceded the first failure", bool(saturation_t and first_failure and saturation_t <= first_failure + 10), 0.15, [e_cpu, e_mem]),
              ("Memory reached >=80% of the container limit", bool(mem_pressure_t), 0.1, [e_mem, e_memlog]),
              ("Containers were OOMKilled or failed health checks", bool(oom_count or e_events.get("Unhealthy")), 0.15,
               list(e_terms.values())[:3] + [e_oom_metric]),
              ("No rollout or ConfigMap change shortly before onset", not rollouts and not cm_changes, 0.1, [e_changes]),
              ("No node-level problems or evictions", not node_issues and not evictions, 0.05, [e_node]),
              ("Failures stopped after traffic returned to baseline", bool(traffic["return_t"] and recovered_t and recovered_t >= traffic["return_t"] - 10), 0.1, [e_rps, e_err]),
          ]),
        H("bad_change", f"A recent deployment/config change to {target_dep} introduced a defect", [
            ("Rollout or ConfigMap change within 30 min before onset", bool(rollouts or cm_changes), 0.35, [e_changes]),
            ("Failures began within 10 min of the most recent change",
             bool(last_change_t and first_failure and 0 <= first_failure - last_change_t <= 600), 0.25, [e_changes]),
            ("Failures began without a traffic increase", not traffic["spike"], 0.25, [e_rps]),
            ("Service was never healthy between the change and the failures", not healthy_after_change, 0.15,
             [e_err, e_journal]),
        ]),
        H("memory_leak", f"Memory leak in {target_dep} independent of load", [
            ("Memory trending upward (>2% of limit/min) at baseline traffic before onset", pre_trend > 0.02, 0.5, [e_mem]),
            ("Containers OOMKilled", oom_count > 0, 0.2, list(e_terms.values())[:2]),
            ("No traffic increase explains the growth", not traffic["spike"], 0.3, [e_rps]),
        ]),
        H("downstream_dependency", f"{target_dep} failing because of a downstream dependency", [
            ("5xx errors while target pods were running and Ready", bool(err_start) and not target_terms and not first_not_ready, 0.5, [e_err, e_journal]),
            ("No CPU or memory saturation on the target", not saturation_t, 0.3, [e_cpu, e_mem]),
            ("No container restarts", not target_terms, 0.2, [e_pst]),
        ]),
        H("node_failure", "Node or cluster-level infrastructure failure", [
            ("Node warnings (NotReady / SystemOOM / eviction threshold)", bool(node_issues), 0.7, [e_node]),
            ("Pods evicted / preempted / unschedulable", bool(evictions), 0.3, [e_node]),
        ]),
    ]
    hyps.sort(key=lambda h: h["score"], reverse=True)
    top, runner = hyps[0], hyps[1]
    confidence = max(0.05, min(0.97, top["score"] * (1 - 0.6 * runner["score"])))
    coverage = sum(1 for s in bundle["sources"] if s["status"] == "ok") / max(1, len(bundle["sources"]))
    confidence = round(confidence * (0.85 + 0.15 * coverage), 2)

    # ---- impact -----------------------------------------------------------------------
    impact_start = first_failure or lat_start
    impact_end = recovered_t or (err_last if err_last and err_last < w_end - 30 else None) or w_end
    total_req = integrate(rps, impact_start, impact_end) if impact_start else 0
    failed_req = integrate(err_rps, impact_start, impact_end) if impact_start else 0
    by_code = {code: round(integrate(pts, impact_start, impact_end)) for code, pts in by_code_series.items()} if impact_start else {}
    dependents = sorted({d["name"] for d in k8s["deployments"] if d["name"] != target_dep and target_service and any(
        target_service["name"] in str(v) for c in d["containers"] for v in c["env"].values())})
    affected_pods = sorted({t["pod"] for t in target_terms} |
                           {e["pod"] for e in journal if e["kind"] == "pod_not_ready" and e["pod"] in target_pods})
    duration = (impact_end - impact_start) if impact_start else 0
    if err_peak and (err_peak >= 0.5 or zero_total >= 60):
        severity = "Critical" if duration > 300 else "High"
    elif err_peak and err_peak >= 0.2:
        severity = "High"
    elif err_peak and err_peak >= thr or target_terms:
        severity = "Medium"
    else:
        severity = "Low"
    impact = {
        "start_t": impact_start, "end_t": impact_end, "ongoing": recovered_t is None,
        "duration_s": round(duration), "peak_error_ratio": err_peak,
        "avg_error_ratio": (failed_req / total_req) if total_req else None,
        "requests_total": round(total_req), "requests_failed": round(failed_req), "failed_by_code": by_code,
        "affected_pods": affected_pods, "pods_total": len(target_pods) or target.get("replicas_desired"),  # pods present during the incident
        "replicas_desired": target.get("replicas_desired"), "min_ready_replicas": min_ready,
        "no_ready_endpoints_s": round(zero_total), "container_terminations": len(target_terms),
        "oom_kills": oom_count, "restarts": len([s for s in all_starts if first_restart and s[0] >= first_restart[0] - 1]),
        "dependent_services": dependents, "user_facing_service": entry,
        "peak_p95_latency_s": lat_peak,
    }

    timeline.sort(key=lambda e: e["t"])
    return {
        "target": {
            "namespace": ns, "deployment": target_dep,
            "service": target_service["name"] if target_service else None,
            "pods": target_pods, "replicas_desired": target.get("replicas_desired"),
            "containers": target.get("containers", []),
        },
        "entry_app": entry,
        "severity": severity,
        "traffic": traffic, "errors": errors, "latency": latency,
        "resources": {f"{p}/{c}": r for (p, c), r in tres.items()},
        "key_times": {
            "traffic_onset": onset, "cpu_saturation": cpu_sat_t, "memory_pressure": mem_pressure_t,
            "first_termination": target_terms[0]["t"] if target_terms else None,
            "first_not_ready": first_not_ready["t"] if first_not_ready else None,
            "error_spike": err_start, "detected": inc.get("detected_at"),
            "traffic_returned": traffic["return_t"], "recovered": recovered_t,
        },
        "terminations": target_terms,
        "k8s_events": event_groups,
        "changes": {"rollouts": rollouts, "configmap_changes": cm_changes, "node_issues": node_issues, "evictions": evictions},
        "log_groups": log_groups,
        "last_logs_before_termination": last_lines,
        "hypotheses": hyps,
        "confidence": confidence,
        "impact": impact,
        "timeline": timeline,
        "evidence": ev.items,
        "source_status": bundle["sources"],
        "_trigger_evidence": ev_trigger,
    }


def _trend(points: Points) -> float:
    """Least-squares slope per minute of a ratio series (used for leak detection)."""
    if len(points) < 6:
        return 0.0
    n = len(points)
    mt = sum(t for t, _ in points) / n
    mv = sum(v for _, v in points) / n
    den = sum((t - mt) ** 2 for t, _ in points)
    return (sum((t - mt) * (v - mv) for t, v in points) / den) * 60 if den else 0.0


def _summarize_logs(logs: dict, dep_of, target_pods, ev: Evidence, ns: str):
    groups: dict[tuple, dict] = {}
    last_lines = {}
    for key, recs in logs.items():
        pod, ctr = key.split("/", 1)
        dep = dep_of(pod)
        prev = [r for r in recs if r.get("instance") == "previous"]
        if prev and pod in target_pods:
            last_lines[key] = [{k: v for k, v in r.items() if k not in ("pod", "container", "instance", "service")}
                               for r in prev[-6:]]
        for r in recs:
            msg = r.get("msg", "")
            level = (r.get("level") or "info").lower()
            if msg == "stats" or r.get("t") is None:
                continue
            g = groups.setdefault((dep, level, msg), {"deployment": dep, "level": level, "msg": msg, "count": 0,
                                                     "first": r["t"], "last": r["t"], "pods": set(), "sample": r,
                                                     "reasons": set()})
            g["count"] += r["count"] if isinstance(r.get("count"), int) else 1
            g["first"], g["last"] = min(g["first"], r["t"]), max(g["last"], r["t"])
            g["pods"].add(pod)
            if r.get("reason"):
                g["reasons"].add(r["reason"])
    out = []
    for g in sorted(groups.values(), key=lambda g: g["first"]):
        g["evidence"] = ev.add(f"log.{g['deployment']}.{g['level']}.{g['msg']}", "logs",
                               f"{g['deployment']} {g['level']} log: \"{g['msg']}\" (x{g['count']})",
                               str({k: v for k, v in g["sample"].items() if k not in ("t", "pod", "container", "instance", "service")})[:400],
                               f"kubectl logs -n {ns} -l app={g['deployment']} [--previous]")
        g["pods"] = sorted(g["pods"])
        g["reasons"] = sorted(g["reasons"])
        g.pop("sample")
        out.append(g)
    for key, lines in last_lines.items():
        ev.add(f"lastlog.{key}", "logs", f"Last log lines before termination ({key}, previous container)",
               str(lines[-3:])[:500], f"kubectl logs -n {ns} {key.split('/')[0]} -c {key.split('/')[1]} --previous")
    return out, last_lines
