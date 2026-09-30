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
    p = argparse.ArgumentParser(prog="investigator", description="Kubernetes incident investigation (Iteration 2)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="verify connectivity to Kubernetes, the entry service, metrics and Slack")
    st = sub.add_parser("status", help="print live health of the watched namespace")
    st.add_argument("--once", action="store_true")
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
        from . import report as rpt
        from . import slack
        if args.cmd == "post":
            with open(args.report_file, encoding="utf-8") as f:
                r = json.load(f)
            print("posted to Slack" if slack.post(settings, rpt.slack_payload(r, args.report_file[:-5] + ".md")) else "NOT posted")
            return
        from .evidence import EvidenceStore
        from .pipeline import diagnose_and_report
        with open(args.evidence_file, encoding="utf-8") as f:
            saved = json.load(f)
        incident = {"id": saved["incident_id"], "detected_at": saved["detected_at"],
                    "signals": [{"text": t} for t in saved["signals"]], "namespace": settings.namespace}
        diagnose_and_report(settings, incident, EvidenceStore.from_dict(saved["store"]),
                            (saved["window"]["start"], saved["window"]["end"]), post_to_slack=args.slack)
        return

    from .kube import Kube
    from .pipeline import connect_metrics
    kube = Kube(settings.kube_context)

    if args.cmd == "check":
        from . import slack
        pods = kube.pods(settings.namespace)
        print(f"Kubernetes : OK - namespace {settings.namespace}: {len(pods)} pods "
              f"({', '.join(sorted({p['app'] or p['name'] for p in pods}))})")
        status, body = kube.service_proxy_get(settings.namespace, settings.entry_service, settings.entry_port,
                                              settings.entry_path)
        print(f"Entry probe: HTTP {status} from {settings.entry_service}{settings.entry_path.split('?')[0]} - {body.strip()[:80]}")
        prom = connect_metrics(settings)
        print(f"Metrics    : {'Prometheus OK (optional)' if prom else 'not used'}")
        print(f"Slack      : {slack.check(settings)}")
        print(f"Probes     : active dependency probes {'enabled' if settings.active_probes else 'disabled'}")
        return

    if args.cmd == "status":
        from .detector import Detector, format_sample
        det = Detector(settings, kube, None)
        while True:
            print(format_sample(det.sample()), flush=True)
            if args.once:
                return
            time.sleep(settings.poll_interval_s)

    prom = connect_metrics(settings)
    if args.cmd == "watch":
        from .pipeline import watch
        try:
            watch(settings, kube, prom, post_to_slack=not args.no_slack, verbose=not args.quiet)
        except KeyboardInterrupt:
            print("\nstopped")
        return

    if args.cmd == "investigate":
        from .pipeline import investigate, new_incident
        now = prom.now() if prom else time.time()
        incident = new_incident([{"kind": "manual", "subject": "system", "t": now,
                                  "text": "manual investigation request (no failure information given)"}],
                                now, settings.namespace)
        investigate(settings, kube, prom, incident, now - args.since, now, post_to_slack=not args.no_slack)


if __name__ == "__main__":
    main()
