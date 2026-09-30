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


def _connect(settings):
    from .kube import Kube
    from .prom import connect
    kube = Kube(settings.kube_context)
    prom = connect(settings)
    return prom, kube


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="investigator", description="Kubernetes incident investigation prototype")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="verify connectivity to Kubernetes, Prometheus, Slack and the LLM")
    sub.add_parser("llm-test", help="verify only the LLM API key/model (no cluster needed)")
    st = sub.add_parser("status", help="print live health of the watched namespace")
    st.add_argument("--once", action="store_true")
    w = sub.add_parser("watch", help="detect incidents, investigate them and post reports")
    w.add_argument("--no-slack", action="store_true")
    w.add_argument("--quiet", action="store_true", help="don't print every sample")
    inv = sub.add_parser("investigate", help="investigate a past time window on demand")
    inv.add_argument("--since", type=_duration, default=_duration("15m"), help="window start, e.g. 15m ago")
    inv.add_argument("--until", type=_duration, default=0.0, help="window end, e.g. 0 (now) or 2m ago")
    inv.add_argument("--no-slack", action="store_true")
    po = sub.add_parser("post", help="post an already-saved report (reports/INC-*.json) to Slack")
    po.add_argument("report_file")
    po.add_argument("--note", default="", help="text appended to the Status field, e.g. 'corrected re-analysis'")
    rp = sub.add_parser("replay", help="re-run analysis/report on a saved *.evidence.json bundle")
    rp.add_argument("evidence_file")
    rp.add_argument("--slack", action="store_true", help="also post the regenerated report")
    args = p.parse_args()
    settings = Settings()

    if args.cmd == "check":
        from . import slack
        from .collector import check_metrics_available
        prom, kube = _connect(settings)
        print(f"Kubernetes : OK — namespace {settings.namespace}: {len(kube.pods(settings.namespace))} pods")
        print(f"Prometheus : OK — {prom.url} (cluster clock offset {prom.clock_offset:+.1f}s); "
              f"series: {check_metrics_available(prom, settings)}")
        print(f"Slack      : {slack.check(settings)}")
        from .llm import ping
        print(f"LLM        : {ping(settings)}")
        return

    if args.cmd == "llm-test":
        from .llm import ping
        print(f"LLM ({settings.llm_provider}): {ping(settings)}")
        return

    if args.cmd == "status":
        from .detector import Detector, format_sample
        prom, kube = _connect(settings)
        det = Detector(settings, prom, kube)
        while True:
            print(format_sample(det.sample()), flush=True)
            if args.once:
                return
            time.sleep(settings.poll_interval_s)

    if args.cmd == "watch":
        from .pipeline import watch
        prom, kube = _connect(settings)
        try:
            watch(settings, prom, kube, post_to_slack=not args.no_slack, verbose=not args.quiet)
        except KeyboardInterrupt:
            print("\nstopped")
        return

    if args.cmd == "investigate":
        from .pipeline import new_incident, run_investigation
        prom, kube = _connect(settings)
        now = prom.now()
        start, end = now - args.since, now - args.until
        incident = new_incident(["manual investigation request"], end, settings.namespace)
        incident["detected_at"] = None  # no detector signal; baselines come from the start of the window
        run_investigation(settings, prom, kube, incident, start, end, post_to_slack=not args.no_slack)
        return

    if args.cmd == "post":
        from . import report as rpt
        from . import slack
        with open(args.report_file, encoding="utf-8") as f:
            report = json.load(f)
        if args.note:
            report["status"] = f"{report['status']} ({args.note})"
        md_path = args.report_file[:-5] + ".md" if args.report_file.endswith(".json") else None
        print("posted to Slack" if slack.post(settings, rpt.slack_payload(report, md_path)) else "NOT posted")
        return

    if args.cmd == "replay":
        from .pipeline import analyze_and_report
        with open(args.evidence_file, encoding="utf-8") as f:
            bundle = json.load(f)["bundle"]
        analyze_and_report(settings, bundle, post_to_slack=args.slack)


if __name__ == "__main__":
    main()
