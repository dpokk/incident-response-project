"""Pipeline with clean stage boundaries:

    Detection -> Evidence collection -> Diagnosis -> Incident report
    [future: Remediation -> Verification]

Each stage only consumes the previous stage's output. The pipeline stops after the report:
no restarts, scaling, config changes or any other corrective action.
"""
import time
from datetime import datetime

from . import report as rpt
from . import slack
from .capabilities import Capabilities
from .capabilities.kubernetes import KubernetesAdapter
from .capabilities.prometheus import PrometheusMetrics
from .collect import collect
from .context import IncidentContext
from .detector import Detector, format_sample
from .planner import plan_and_collect
from .diagnosis import diagnose
from .evidence import EvidenceStore
from .kube import PodJournal


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def new_incident(signals: list[dict], detected_at: float, namespace: str) -> dict:
    return {"id": "INC-" + datetime.fromtimestamp(detected_at).strftime("%Y%m%d-%H%M%S"),
            "detected_at": detected_at, "namespace": namespace, "signals": list(signals)}


def connect_metrics(settings):
    """Optional Prometheus; returns None (and says so) if it is disabled or unreachable."""
    if not settings.prometheus_enabled:
        return None
    try:
        from .prom import connect
        return connect(settings)
    except Exception as exc:  # noqa: BLE001
        log(f"metrics source unavailable ({exc}); continuing without metrics")
        return None


def investigate(settings, kube, prom, incident: dict, start: float, end: float,
                post_to_slack: bool = True, ongoing: bool | None = None) -> dict:
    log(f"Investigating {incident['id']} (window {rpt.hms(start)}-{rpt.hms(end)}); the investigator is not told what failed")
    store = EvidenceStore()
    caps = build_capabilities(settings, kube, prom, store)
    entry = (settings.entry_service, settings.entry_port, settings.entry_path)
    log(f"Stage 1/3: evidence collection, strategy '{settings.investigation_strategy}' (providers: {caps.resources.name}"
        + (f", {caps.metrics.name}" if caps.metrics else "") + ")")
    if settings.investigation_strategy == "exhaustive":
        collect(caps, incident, start, end, entry=entry, metrics_target=settings.entry_app, log=log)
    else:
        ctx = IncidentContext.from_incident(incident, start, end, entry=entry, metrics_target=settings.entry_app)
        log(f"  incident context: suspects {ctx.suspects or 'none'} from {len(ctx.signals)} signal(s)")
        plan_and_collect(caps, ctx, log=log)
    log(f"  {len(store.facts)} facts collected with {sum(1 for s in store.trace if s['step'] == 'capability')} "
        f"capability calls")
    return diagnose_and_report(settings, incident, store, (start, end), post_to_slack, ongoing)


def build_capabilities(settings, kube, prom, store: EvidenceStore) -> Capabilities:
    """Assemble the capability layer: Kubernetes for resources, Prometheus (if reachable) for metrics."""
    resources = KubernetesAdapter(kube, settings.namespace, journal_path=settings.state_dir / "pod_journal.jsonl",
                                  active_probes=settings.active_probes)
    metrics = PrometheusMetrics(prom, settings.namespace) if prom is not None else None
    return Capabilities(resources, store, metrics)


def diagnose_and_report(settings, incident: dict, store: EvidenceStore, window: tuple,
                        post_to_slack: bool = True, ongoing: bool | None = None) -> dict:
    log("Stage 2/3: diagnosis")
    dx = diagnose(store)
    log(f"  -> {dx['category_label']} in {dx['affected_component']} (confidence {dx['confidence']:.0%})")
    log("Stage 3/3: incident report")
    report = rpt.build(incident, store, dx, window, ongoing)
    paths = rpt.save(report, store, dx, settings.reports_dir)
    print("\n" + rpt.render_text(report) + "\n", flush=True)
    log(f"  saved {paths['text']} (+ .md, .json, .evidence.json)")
    if post_to_slack and settings.slack_configured:
        if slack.post(settings, rpt.slack_payload(report, paths["markdown"]), log=log):
            log("  report posted to Slack")
    log("Done. No remediation performed (out of scope).")
    return {"report": report, "paths": paths}


def watch(settings, kube, prom, post_to_slack: bool = True, verbose: bool = True) -> None:
    journal = PodJournal(kube, settings.namespace, settings.state_dir / "pod_journal.jsonl",
                         clock=prom.now if prom else time.time)
    journal.start()
    det = Detector(settings, kube, prom, clock=prom.now if prom else time.time)
    det.sample()
    log(f"Watching namespace '{settings.namespace}' every {settings.poll_interval_s:.0f}s "
        f"(restarts, waiting containers, NotReady pods, synthetic requests to {settings.entry_service}, error logs"
        + (", Prometheus 5xx" if prom else "") + ")")
    incident, investigate_at, last_unhealthy = None, 0.0, 0.0
    while True:
        time.sleep(settings.poll_interval_s)
        try:
            s = det.sample()
        except Exception as exc:  # noqa: BLE001 - keep watching through transient API errors
            log(f"sample failed: {exc}")
            continue
        if verbose:
            print(format_sample(s) + ("   <-- " + "; ".join(x["text"] for x in s["signals"])[:220] if s["signals"] else ""),
                  flush=True)
        if s["unhealthy"]:
            last_unhealthy = s["t"]
        if incident is None:
            if s["signals"]:
                incident = new_incident(s["signals"], s["t"], settings.namespace)
                investigate_at = s["t"] + settings.investigate_delay_s
                log(f"INCIDENT DETECTED {incident['id']}: " + "; ".join(x["text"] for x in s["signals"])[:300])
                log(f"Collecting symptoms for {settings.investigate_delay_s:.0f}s before investigating")
                if post_to_slack and settings.slack_post_detection and settings.slack_configured:
                    slack.post(settings, rpt.detection_payload(incident, settings.namespace), log=log)
            continue
        seen = {(x["kind"], x["subject"]) for x in incident["signals"]}
        new = [x for x in s["signals"] if (x["kind"], x["subject"]) not in seen]
        if incident.get("reported") and new:
            # Different symptoms after the report: treat as a new incident rather than folding it in.
            log(f"New symptoms after {incident['id']} was reported; opening a new incident")
            incident = new_incident(s["signals"], s["t"], settings.namespace)
            investigate_at = s["t"] + settings.investigate_delay_s
            log(f"INCIDENT DETECTED {incident['id']}: " + "; ".join(x["text"] for x in s["signals"])[:300])
            continue
        incident["signals"] += new[:10 - len(incident["signals"])]
        if not incident.get("reported") and s["t"] >= investigate_at:
            try:
                investigate(settings, kube, prom, incident, incident["detected_at"] - settings.lookback_s, s["t"],
                            post_to_slack, ongoing=s["unhealthy"])
            except Exception as exc:  # noqa: BLE001
                log(f"investigation failed: {exc!r}")
            incident["reported"] = True
            if not s["unhealthy"]:
                log(f"Incident {incident['id']} was transient and has already cleared; back to watching")
                incident = None
                continue
            log("Waiting for the system to recover (recovery is manual in this iteration)")
        elif incident.get("reported") and s["t"] - last_unhealthy >= settings.resolve_stable_s:
            log(f"Incident {incident['id']} symptoms have cleared; back to watching")
            if post_to_slack and settings.slack_configured:
                slack.post(settings, rpt.resolved_payload(incident, settings.namespace), log=log)
            incident = None
