"""CLI entry point: python -m investigator <command>"""
import argparse
import json
import re
import sys
import time

from .config import Settings


def _duration(s: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smh]?)", s.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration {s!r} (use e.g. 90s, 15m, 1h)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="investigator", description="Evidence-driven incident investigation (Iteration 3)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="verify the resource provider, entry service, metrics and Slack")
    st = sub.add_parser("status", help="print live health of the watched namespace")
    st.add_argument("--once", action="store_true")
    sub.add_parser("record", help="only record evidence history (no detection), e.g. alongside manual investigations")
    sub.add_parser("review", help="listen for Slack Approve/Reject (recorded) and Execute (checked, then applied) "
                                  "interactions over Socket Mode")
    w = sub.add_parser("watch", help="detect incidents, investigate them and report")
    w.add_argument("--no-slack", action="store_true")
    w.add_argument("--quiet", action="store_true", help="don't print every sample")
    inv = sub.add_parser("investigate", help="investigate the current state / a past window on demand")
    inv.add_argument("--since", type=_duration, default=_duration("10m"), help="window start, e.g. 10m ago")
    inv.add_argument("--no-slack", action="store_true")
    rp = sub.add_parser("replay", help="re-run diagnosis on a saved reports/*.evidence.json")
    rp.add_argument("evidence_file")
    rp.add_argument("--slack", action="store_true")
    po = sub.add_parser("post", help="post a saved report (reports/INC-*.json) to Slack")
    po.add_argument("report_file")
    args = p.parse_args()
    settings = Settings()

    if args.cmd in ("replay", "post"):
        from . import slack
        from . import slack_view
        if args.cmd == "post":
            with open(args.report_file, encoding="utf-8") as f:
                r = json.load(f)
            print("posted to Slack" if slack.post(settings, slack_view.investigation_message(r)) else "NOT posted")
            return
        from .evidence import EvidenceStore
        from .pipeline import diagnose_and_report
        with open(args.evidence_file, encoding="utf-8") as f:
            saved = json.load(f)
        incident = {"id": saved["incident_id"], "detected_at": saved["detected_at"],
                    "signals": [{"text": t} for t in saved["signals"]], "namespace": settings.namespace}
        from . import legacy
        if legacy.is_legacy(saved["store"]):
            print("(evidence saved before Iteration 3 step 4: upgrading its vocabulary for today's engine)")
            saved["store"] = legacy.upgrade(saved["store"])
        diagnose_and_report(settings, incident, EvidenceStore.from_dict(saved["store"]),
                            (saved["window"]["start"], saved["window"]["end"]), post_to_slack=args.slack)
        return

    if args.cmd == "review":
        # Review records decisions. Execute (a separate step, Iteration 7) needs the cluster: connect if possible;
        # without a connection the listener still records decisions and offers no Execute.
        from . import slack_app
        from .pipeline import execution_service, log, review_service
        review = review_service(settings)
        execution = None
        try:
            from . import providers as providers_mod
            execution = execution_service(settings, providers_mod.connect(settings, log=log), review)
        except Exception as exc:  # noqa: BLE001
            log(f"cluster not reachable ({type(exc).__name__}): review only, Execute unavailable")
        try:
            slack_app.start(settings, review, log=log, block=True, execution=execution)
        except KeyboardInterrupt:
            print("\nstopped")
        return

    from . import providers as providers_mod
    from .pipeline import log
    providers = providers_mod.connect(settings, log=log)

    if args.cmd == "check":
        from . import slack
        r = providers.new_resources()
        comps = r.list_components()
        print(f"Resources  : OK - {r.name} ({r.scope}): {len(comps)} components ({', '.join(sorted(comps))})")
        res = r.probe_request(settings.entry_service, settings.entry_port, settings.entry_path)
        print(f"Entry probe: HTTP {res.status} from {settings.entry_service}{settings.entry_path.split('?')[0]} - "
              f"{res.body.strip()[:80]}")
        print(f"Metrics    : {providers.metrics.name + ' OK (optional)' if providers.metrics else 'not used'}"
              + (f", clock offset {providers.clock_offset:+.1f}s" if providers.metrics else ""))
        print(f"Slack      : {slack.check(settings)}")
        print(f"Probes     : active dependency probes {'enabled' if settings.active_probes else 'disabled'}")
        print(f"Strategy   : {settings.investigation_strategy}")
        if providers.history is not None:
            s = providers.history.stats()
            print(f"History    : {settings.history_path} ({settings.history_retention_h:g}h retention) - "
                  f"{s['sessions']} recording session(s), {s['log_lines']} log lines, {s['lifecycle']} lifecycle records, "
                  f"{s['events']} events, {s['object_versions']} object versions")
        else:
            print("History    : disabled (HISTORY_ENABLED=false): evidence that the platform forgets is not retained")
        return

    if args.cmd == "record":
        if providers.history is None:
            print("Evidence history is disabled (HISTORY_ENABLED=false); nothing to record.")
            return
        r = providers.new_resources()
        r.start_background_recording()
        log(f"Recording evidence history for {providers.describe()} into {settings.history_path} (Ctrl+C to stop)")
        try:
            while True:
                time.sleep(60)
                s = providers.history.stats()
                log(f"history: {s['log_lines']} log lines, {s['lifecycle']} lifecycle records, {s['events']} events, "
                    f"{s['object_versions']} object versions, {s['log_gaps']} dropped-line markers")
        except KeyboardInterrupt:
            print("\nstopped")
        return

    if args.cmd == "status":
        from .detector import Detector, format_sample
        det = Detector(settings, providers.new_resources(), None, clock=providers.clock)
        while True:
            print(format_sample(det.sample()), flush=True)
            if args.once:
                return
            time.sleep(settings.poll_interval_s)

    if args.cmd == "watch":
        from .pipeline import watch
        try:
            watch(settings, providers, post_to_slack=not args.no_slack, verbose=not args.quiet)
        except KeyboardInterrupt:
            print("\nstopped")
        return

    if args.cmd == "investigate":
        from .pipeline import investigate, new_incident
        now = providers.clock()
        incident = new_incident([{"kind": "manual", "subject": "system", "t": now,
                                  "text": "manual investigation request (no failure information given)"}],
                                now, settings.namespace)
        investigate(settings, providers, incident, now - args.since, now, post_to_slack=not args.no_slack)


if __name__ == "__main__":
    main()
