"""End-to-end investigation pipeline and the watch loop that drives it."""
import re
import time
from datetime import datetime

from . import report as rpt
from . import slack
from .analysis import analyze
from .collector import collect
from .detector import Detector, format_sample
from .kube import PodJournal
from .llm import generate_narrative


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def new_incident(triggers: list[str], detected_at: float, namespace: str) -> dict:
    return {"id": "INC-" + datetime.fromtimestamp(detected_at).strftime("%Y%m%d-%H%M%S"),
            "detected_at": detected_at, "triggers": triggers, "namespace": namespace}


def run_investigation(settings, prom, kube, incident: dict, start: float, end: float,
                      post_to_slack: bool = True) -> dict:
    log(f"Investigating {incident['id']} — window "
        f"{rpt.hms(start)}–{rpt.hms(end)} ({int(end - start)}s)")
    log("Step 1/5: collecting evidence")
    bundle = collect(settings, prom, kube, incident, start, end,
                     journal_path=settings.state_dir / "pod_journal.jsonl", log=log)
    return analyze_and_report(settings, bundle, post_to_slack)


def analyze_and_report(settings, bundle: dict, post_to_slack: bool = True) -> dict:
    log("Step 2/5: correlating evidence, reconstructing timeline, scoring hypotheses")
    a = analyze(bundle, settings)
    top = a["hypotheses"][0]
    log(f"  target workload: {a['target']['namespace']}/{a['target']['deployment']}; "
        f"top hypothesis: {top['id']} (score {top['score']:.2f}); {len(a['timeline'])} timeline events")
    log("Step 3/5: generating narrative")
    narrative, generator = generate_narrative(bundle, a, settings, log=log)
    log("Step 4/5: building report")
    report = rpt.build_report(bundle, a, narrative, generator)
    paths = rpt.save(report, bundle, a, settings.reports_dir)
    log(f"  saved {paths['markdown']}")
    print("\n" + rpt.render_markdown(report) + "\n", flush=True)
    if post_to_slack:
        log("Step 5/5: posting report to Slack")
        if slack.post(settings, rpt.slack_payload(report, paths["markdown"]), log=log):
            log("  report posted to Slack")
    else:
        log("Step 5/5: Slack posting disabled (--no-slack)")
    return {"report": report, "paths": paths}


def watch(settings, prom, kube, post_to_slack: bool = True, verbose: bool = True) -> None:
    journal = PodJournal(kube, settings.namespace, settings.state_dir / "pod_journal.jsonl", clock=prom.now)
    journal.start()
    det = Detector(settings, prom, kube)
    det.sample()  # prime restart counters
    log(f"Watching namespace '{settings.namespace}' every {settings.poll_interval_s:.0f}s "
        f"(5xx>{settings.error_rate_threshold * 100:.0f}%, CPU>{settings.cpu_threshold * 100:.0f}%, "
        f"mem>{settings.memory_threshold * 100:.0f}% of limit, or any pod restart)")
    incident, last_bad = None, 0.0
    while True:
        time.sleep(settings.poll_interval_s)
        try:
            s = det.sample()
        except Exception as exc:  # noqa: BLE001 - keep watching through transient API errors
            log(f"sample failed: {exc}")
            continue
        triggers = det.evaluate(s)
        if verbose:
            print(format_sample(s) + (f"   <-- {'; '.join(triggers)}" if triggers else ""), flush=True)
        unhealthy = bool(triggers or s["not_ready"] or s["error_ratio"] >= 0.01)
        if incident is None:
            if triggers:
                incident = new_incident(triggers, s["t"], settings.namespace)
                last_bad = s["t"]
                log(f"INCIDENT DETECTED {incident['id']}: {'; '.join(triggers)}")
                if post_to_slack and settings.slack_post_detection and settings.slack_configured:
                    slack.post(settings, rpt.detection_payload(incident, settings.namespace), log=log)
                log(f"Monitoring until stable for {settings.resolve_stable_s:.0f}s before running the investigation")
            continue
        for t in triggers:  # keep one trigger per kind ("HTTP 5xx rate", "memory ..."), ignoring the numbers
            kind = re.sub(r"[\d.]+%?", "#", t)
            if not any(re.sub(r"[\d.]+%?", "#", x) == kind for x in incident["triggers"]) and len(incident["triggers"]) < 8:
                incident["triggers"].append(t)
        if unhealthy:
            last_bad = s["t"]
        stable_for = s["t"] - last_bad
        waited = s["t"] - incident["detected_at"]
        if stable_for >= settings.resolve_stable_s or waited >= settings.max_incident_wait_s:
            if waited >= settings.max_incident_wait_s:
                log("Incident still active after max wait; investigating now")
            start = incident["detected_at"] - settings.lookback_s
            end = s["t"]
            try:
                run_investigation(settings, prom, kube, incident, start, end, post_to_slack)
            except Exception as exc:  # noqa: BLE001
                log(f"investigation failed: {exc!r}")
                raise
            incident = None
            det.streak = {"errors": 0, "cpu": 0}
            log("Back to watching")
