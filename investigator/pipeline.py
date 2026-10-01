"""Pipeline with clean stage boundaries:

    Detection -> Evidence collection -> Diagnosis -> Incident report -> Remediation plan (proposal only)
    -> Slack incident thread + human review (decisions recorded by review.py)
    -> [only on a separate, explicit Execute by an authorised person] executor.py: checks, one typed change,
       verification (Iteration 7)

Each stage only consumes the previous stage's output. The investigation pipeline itself never changes anything: it
stops after the plan is presented for review. A change happens only through the execution service, which the
Slack listener calls when an authorised person clicks Execute on an approved action.
"""
import time
from datetime import datetime

from . import report as rpt
from . import slack
from .review import ReviewService
from .execution_policy import ExecutionPolicy
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
                post_to_slack: bool = True, ongoing: bool | None = None, execution=None) -> dict:
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
    return diagnose_and_report(settings, incident, store, (start, end), post_to_slack, ongoing, execution)


def diagnose_and_report(settings, incident: dict, store: EvidenceStore, window: tuple,
                        post_to_slack: bool = True, ongoing: bool | None = None, execution=None) -> dict:
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
                                       digest, settings.namespace, time.time(), execution=execution,
                                       stale_after_s=_policy_max_age(settings)):
            log("  investigation and plan posted to the incident's Slack thread")
    log("Done. No remediation performed: the plan awaits human review.")
    return {"report": report, "paths": paths, "digest": digest}


def review_service(settings) -> ReviewService:
    return ReviewService(settings.reviews_path, settings.slack_approvers)


def _policy_max_age(settings) -> float | None:
    try:
        return ExecutionPolicy.load(settings.execution_policy_path).max_plan_age_s
    except Exception:  # noqa: BLE001 - display only; the executor loads (and enforces) the policy itself
        return None


def execution_service(settings, providers: Providers, review: ReviewService):
    """The execution service (Iteration 7): the only holder of the cluster writer. Built here, at the composition
    root, and handed to the Slack listener; investigation code never receives it."""
    from .execution_store import ExecutionStore
    from .executor import ExecutionService
    store = ExecutionStore(settings.reviews_path, clock=providers.clock)
    for rec in store.recover_interrupted():
        log(f"execution {rec['execution_id']} ({rec['incident_id']}) was interrupted earlier: {rec['message']}")
    return ExecutionService(
        review, store, ExecutionPolicy.load(settings.execution_policy_path),
        providers.new_actuator() if providers.new_actuator else None,
        capabilities=lambda: providers.capabilities(EvidenceStore(clock=providers.clock)), clock=providers.clock,
        log=log, poll_s=settings.poll_interval_s,
        entry=(settings.entry_service, settings.entry_port, settings.entry_path))


def without_expected_effects(signals: list[dict], execution, t: float, logged: set) -> list[dict]:
    """Drop signals that are the expected effects of a change being executed/verified (Iteration 7): a rollout's
    restarts must not open a duplicate incident. Scoped by the execution service to that incident's components and
    to the execution + verification period; everything else is detected as usual."""
    if execution is None:
        return signals
    out = []
    for x in signals:
        inc = execution.expected_effect(x["subject"], t)
        if inc is None:
            out.append(x)
        elif (inc, x["subject"]) not in logged:
            logged.add((inc, x["subject"]))
            log(f"{x['kind']} on {x['subject']}: expected effect of the change being executed for {inc}; not opened "
                f"as a new incident (verification is observing it)")
    return out


def watch(settings, providers: Providers, post_to_slack: bool = True, verbose: bool = True) -> None:
    recorder = providers.new_resources()
    recorder.start_background_recording()   # evidence history recorder (or the older pod journal)
    execution = None
    if post_to_slack and settings.slack_app_token:
        try:                                 # human review (and, separately, Execute) in the incident threads
            from . import slack_app
            review = review_service(settings)
            execution = execution_service(settings, providers, review)
            slack_app.start(settings, review, log=log, execution=execution)
        except Exception as exc:  # noqa: BLE001 - review is optional; watching continues without it
            log(f"Slack review listener not started: {type(exc).__name__}: {exc}")
            execution = None
    suppressed_logged: set = set()
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
        signals = without_expected_effects(s["signals"], execution, s["t"], suppressed_logged)
        if incident is None:
            if signals:
                incident = new_incident(signals, s["t"], settings.namespace)
                investigate_at = s["t"] + settings.investigate_delay_s
                log(f"INCIDENT DETECTED {incident['id']}: " + "; ".join(x["text"] for x in signals)[:300])
                log(f"Collecting symptoms for {settings.investigate_delay_s:.0f}s before investigating")
                if post_to_slack and settings.slack_post_detection and settings.slack_configured:
                    slack.publish_detection(slack.Transport(settings, log), slack.ThreadRegistry(settings.slack_threads_path),
                                            incident, settings.namespace)
            continue
        seen = {(x["kind"], x["subject"]) for x in incident["signals"]}
        new = [x for x in signals if (x["kind"], x["subject"]) not in seen]
        if incident.get("reported") and new:
            # Different symptoms after the report: treat as a new incident rather than folding it in.
            log(f"New symptoms after {incident['id']} was reported; opening a new incident")
            incident = new_incident(signals, s["t"], settings.namespace)
            investigate_at = s["t"] + settings.investigate_delay_s
            log(f"INCIDENT DETECTED {incident['id']}: " + "; ".join(x["text"] for x in signals)[:300])
            continue
        incident["signals"] += new[:10 - len(incident["signals"])]
        if not incident.get("reported") and s["t"] >= investigate_at:
            try:
                investigate(settings, providers, incident, incident["detected_at"] - settings.lookback_s, s["t"],
                            post_to_slack, ongoing=s["unhealthy"], execution=execution)
            except Exception as exc:  # noqa: BLE001
                log(f"investigation failed: {exc!r}")
            incident["reported"] = True
            if not s["unhealthy"]:
                log(f"Incident {incident['id']} was transient and has already cleared; back to watching")
                incident = None
                continue
            log("Waiting for the system to recover (an approved plan can be executed from Slack; otherwise recover manually)")
        elif incident.get("reported") and s["t"] - last_unhealthy >= settings.resolve_stable_s:
            log(f"Incident {incident['id']} symptoms have cleared; back to watching")
            if post_to_slack and settings.slack_configured:
                slack.publish_resolved(slack.Transport(settings, log), slack.ThreadRegistry(settings.slack_threads_path),
                                       review_service(settings), incident, settings.namespace, execution)
            incident = None
