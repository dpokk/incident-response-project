"""Pipeline with clean stage boundaries:

    Detection -> Evidence collection -> Diagnosis -> Incident report -> Remediation plan (proposal only)
    -> Slack incident thread + human review (decisions recorded by review.py)
    [future: approved execution + verification (Iteration 7)]

Each stage only consumes the previous stage's output. The pipeline stops after the plan is presented for review:
no restarts, scaling, config changes or any other corrective action. A human decision is recorded, never executed.
"""
import time
from datetime import datetime

from . import report as rpt
from . import slack
from .review import ReviewService
from .collect import collect
from .context import IncidentContext
from .detector import Detector, format_sample
from .planner import plan_and_collect
from .diagnosis import diagnose
from .evidence import EvidenceStore
from .remediation import plan_remediation
from .providers import Providers


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def new_incident(signals: list[dict], detected_at: float, namespace: str) -> dict:
    return {"id": "INC-" + datetime.fromtimestamp(detected_at).strftime("%Y%m%d-%H%M%S"),
            "detected_at": detected_at, "namespace": namespace, "signals": list(signals)}


def investigate(settings, providers: Providers, incident: dict, start: float, end: float,
                post_to_slack: bool = True, ongoing: bool | None = None) -> dict:
    log(f"Investigating {incident['id']} (window {rpt.hms(start)}-{rpt.hms(end)}); the investigator is not told what failed")
    store = EvidenceStore(clock=providers.clock)
    caps = providers.capabilities(store)
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


def diagnose_and_report(settings, incident: dict, store: EvidenceStore, window: tuple,
                        post_to_slack: bool = True, ongoing: bool | None = None) -> dict:
    log("Stage 2/3: diagnosis")
    dx = diagnose(store)
    log(f"  -> {dx['category_label']} in {dx['affected_component']} (confidence {dx['confidence']:.0%})")
    log("Stage 3/4: incident report")
    report = rpt.build(incident, store, dx, window, ongoing)
    # Plans only: the planner gets the finished investigation, no capabilities and no provider - it cannot act.
    log("Stage 4/4: remediation plan (a proposal for human review; nothing is executed)")
    plan = plan_remediation(dx, report["reconstruction"], report["impact"], store, incident["id"])
    report["remediation_plan"] = plan.to_dict()
    log(f"  -> {plan.assessment.value}: {len([a for a in plan.actions if a.type.value != 'investigate_further'])} "
        f"proposed change(s), requires human approval; not executed")
    paths = rpt.save(report, store, dx, settings.reports_dir)
    print("\n" + rpt.render_text(report) + "\n", flush=True)
    log(f"  saved {paths['text']} (+ .md, .json, .evidence.json)")
    # Present the plan for human review: the review store binds any later decision to this exact plan (digest).
    review = review_service(settings)
    digest = review.register_plan(incident["id"], report["remediation_plan"], (report.get("window") or {}).get("end"))
    log(f"  plan registered for review (digest {digest[:12]})")
    if post_to_slack and settings.slack_configured:
        transport = slack.Transport(settings, log)
        if slack.publish_investigation(transport, slack.ThreadRegistry(settings.slack_threads_path), report, review,
                                       digest, settings.namespace, time.time()):
            log("  investigation and plan posted to the incident's Slack thread")
    log("Done. No remediation performed: the plan awaits human review.")
    return {"report": report, "paths": paths, "digest": digest}


def review_service(settings) -> ReviewService:
    return ReviewService(settings.reviews_path, settings.slack_approvers)


def watch(settings, providers: Providers, post_to_slack: bool = True, verbose: bool = True) -> None:
    recorder = providers.new_resources()
    recorder.start_background_recording()   # evidence history recorder (or the older pod journal)
    if post_to_slack and settings.slack_app_token:
        try:                                 # human review of plans in the incident threads (records decisions only)
            from . import slack_app
            slack_app.start(settings, review_service(settings), log=log)
        except Exception as exc:  # noqa: BLE001 - review is optional; watching continues without it
            log(f"Slack review listener not started: {type(exc).__name__}: {exc}")
    det = Detector(settings, providers.new_resources(), providers.metrics, clock=providers.clock)
    det.sample()
    log(f"Watching {providers.describe()} every {settings.poll_interval_s:.0f}s "
        f"(restarts, waiting processes, NotReady instances, synthetic requests to {settings.entry_service}, error logs"
        + (", 5xx ratio" if providers.metrics else "") + ")")
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
                    slack.publish_detection(slack.Transport(settings, log), slack.ThreadRegistry(settings.slack_threads_path),
                                            incident, settings.namespace)
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
                investigate(settings, providers, incident, incident["detected_at"] - settings.lookback_s, s["t"],
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
                slack.publish_resolved(slack.Transport(settings, log), slack.ThreadRegistry(settings.slack_threads_path),
                                       review_service(settings), incident, settings.namespace)
            incident = None
