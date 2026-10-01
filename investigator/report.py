"""Structured incident report: one format for every failure category.

Observed facts (what the system showed) are kept separate from the diagnosis (what we conclude),
and every diagnostic statement cites the fact IDs it rests on.
"""
import json
from datetime import datetime
from pathlib import Path

from .evidence import EvidenceStore
from .timeline import reconstruct

TIMELINE_KINDS = {"detection_signal", "process_terminated", "event", "log_signature", "log_exception",
                  "change", "config_changed", "metric_traffic_change", "metric_error_ratio", "metric_memory_high",
                  "log_tail_before_exit", "configuration_change", "past_instance", "metric_error_ratio_recovered"}
COVERAGE_KINDS = ("evidence_coverage", "evidence_gap")


def hms(t) -> str:
    return datetime.fromtimestamp(t).astimezone().strftime("%H:%M:%S") if t else "-"


def _rcc(r: dict) -> str:
    """Root-cause component: where the cause is (vs the affected component, where the failure surfaced)."""
    c = r.get("root_cause_component")
    return f"{c['name']} ({c['relation']})" if c else "not determined"


def when(entry: dict) -> str:
    """Timeline time column. Never shows more precision than the evidence has: entries that began before the
    window get no start time, bounded ones show their latest possible time, observed ones "by" that time."""
    basis = entry.get("t_basis")
    if basis == "before_window":
        return "before window"
    if basis == "bounded":
        return f"by {hms(entry['t'])}"
    if basis == "observed":
        return f"seen {hms(entry['t'])}"
    return hms(entry["t"])


def build(incident: dict, store: EvidenceStore, dx: dict, window: tuple, ongoing: bool | None) -> dict:
    facts = {f.id: f for f in store.facts}

    def fx(ids):
        return [{"id": i, "source": facts[i].source, "text": facts[i].text, "t": facts[i].t} for i in ids if i in facts]

    ws = next((w for w in dx["component_summary"] if w["component"] == dx["affected_component"]), {})
    status_fact = next(iter(store.find(kind="component_status", subject=f"component/{dx['affected_component']}")), None)
    # Things that began before the window come first and are labelled as such (no invented start time).
    timeline = sorted((f for f in store.facts if f.t and f.kind in TIMELINE_KINDS and window[0] - 60 <= f.t <= window[1] + 5),
                      key=lambda f: (f.t_basis != "before_window", f.t))
    return {
        "id": incident["id"],
        "detected_at": incident.get("detected_at"),
        "detection_signals": [s["text"] for s in incident.get("signals", [])],
        "investigation_status": ("Diagnosed - impact ongoing" if ongoing else
                                 "Diagnosed - system has recovered" if ongoing is False else "Diagnosed"),
        "remediation": "None performed (out of scope for this iteration; recover manually)",
        "affected_component": {
            "name": dx["affected_component"],
            "namespace": incident.get("namespace"),
            "status": status_fact.text if status_fact else ws.get("status"),
            "instances": status_fact.data.get("instances", []) if status_fact else [],
        },
        "failure_category": dx["category"],
        "failure_category_label": dx["category_label"],
        "root_cause_component": dx.get("root_cause_component"),
        "dependencies": dx["dependencies"],
        "impacted_components": dx["impacted_components"],
        "observed_symptoms": fx(dx["symptoms"]),
        "evidence": fx(dx["evidence"]),
        "diagnosis": [{"statement": r["statement"], "facts": r["facts"]} for r in dx["reasoning"]],
        "likely_root_cause": dx["root_cause"],
        "contributing_factors": dx["contributing_factors"],
        "correlations": dx.get("correlations", []),
        "confidence": dx["confidence"],
        "confidence_label": dx["confidence_label"],
        "alternatives_considered": dx["alternatives"],
        "timeline": [{"t": f.t, "t_basis": f.t_basis, "id": f.id, "text": f.text, "origin": f.origin}
                     for f in _dedupe(timeline)][:30],
        "reconstruction": reconstruct(store, dx, window),
        "evidence_coverage": [{"id": f.id, "kind": f.kind, "text": f.text} for f in store.facts if f.kind in COVERAGE_KINDS],
        "retained_facts": sum(1 for f in store.facts if f.origin in ("retained", "mixed")),
        "components": dx["component_summary"],
        "investigation_trace": _trace_summary(store),
        "window": {"start": window[0], "end": window[1]},
        "facts_collected": len(store.facts),
        "generated_by": "rule-based diagnosis engine (no LLM)",
    }


def _dedupe(facts):
    seen = set()
    for f in facts:
        key = (f.kind, f.subject, f.text[:60])
        if key not in seen:
            seen.add(key)
            yield f


def _trace_summary(store: EvidenceStore) -> list[str]:
    out = []
    for s in store.trace:
        if s["step"] in ("capability", "tool"):  # "tool": evidence saved before Iteration 3
            args = ", ".join(s.get("args", []) + [f"{k}={v}" for k, v in s.get("kwargs", {}).items()])
            via = f" via {s['provider']}" if s.get("provider") else ""
            out.append(f"  {s['detail']}({args}){via} -> {s.get('summary')} [{s.get('status')}]")
        else:
            out.append(f"{s['step']}: {s['detail']}")
    return out


# --------------------------------------------------------------------------- renderers

def render_text(r: dict) -> str:
    L = ["=" * 78, "INCIDENT REPORT", "=" * 78,
         f"Incident ID:          {r['id']}",
         f"Detected at:          {hms(r['detected_at'])}   Window: {hms(r['window']['start'])}-{hms(r['window']['end'])}",
         f"Investigation status: {r['investigation_status']}",
         f"Remediation:          {r['remediation']}", "",
         f"Affected component:   {r['affected_component']['namespace']}/{r['affected_component']['name']}  "
         f"({r['affected_component']['status']})",
         f"Failure category:     {r['failure_category_label']}",
         f"Root-cause component: {_rcc(r)}"]
    if r["dependencies"]:
        for d in r["dependencies"]:
            L.append(f"Dependency involved:  {d['type']} at {d['endpoint']} (configured via {d['variable']} from "
                     f"{d['source']}) - {d['state']}")
    else:
        L.append("Dependency involved:  none implicated")
    if r["impacted_components"]:
        L.append(f"Also impacted:        {', '.join(r['impacted_components'])}")
    L += ["", "OBSERVED SYMPTOMS (facts)"] + [f"  - [{s['id']}] {s['text']}" for s in r["observed_symptoms"]]
    L += ["", "EVIDENCE (observed facts, with source)"] + [f"  - [{e['id']}] ({e['source']}) {e['text']}" for e in r["evidence"]]
    L += ["", "DIAGNOSIS (interpretation of the evidence)"]
    L += [f"  {i}. {d['statement']}  [{', '.join(d['facts'])}]" for i, d in enumerate(r["diagnosis"], 1)]
    L += ["", "LIKELY ROOT CAUSE", f"  {r['likely_root_cause']}"]
    if r["contributing_factors"]:
        L += ["", "CONTRIBUTING FACTORS"] + [f"  - {c['statement']}  [{', '.join(c['facts'])}]" for c in r["contributing_factors"]]
    if r.get("correlations"):
        mark = {True: "linked", False: "not linked", None: "not established"}
        L += ["", "CORRELATIONS (checked against order, component and call path)"]
        L += [f"  - [{mark[c['linked']]}] {c['statement']}  [{', '.join(c['facts'])}]" for c in r["correlations"]]
    L += ["", f"CONFIDENCE: {r['confidence_label']} ({r['confidence']:.0%})", "",
          "ALTERNATIVES CONSIDERED"]
    L += [f"  - {a['label']} in {a['component']} (score {a['score']:.2f}): {a['why_not']}" for a in r["alternatives_considered"]]
    L += reconstruction_lines(r.get("reconstruction"))
    L += ["", "TIMELINE (observed entries)"] + [f"  {when(t):>13}  {t['text'][:150]}" for t in r["timeline"]]
    if r.get("evidence_coverage"):
        L += ["", "EVIDENCE COVERAGE AND LIMITATIONS"] + [f"  - [{c['id']}] {c['text']}" for c in r["evidence_coverage"]]
    L += ["", f"Investigation: {r['facts_collected']} facts collected ({r.get('retained_facts', 0)} from retained "
          f"history) by {sum(1 for x in r['investigation_trace'] if x.startswith('  '))} "
          f"capability calls; generated by {r['generated_by']}.", "=" * 78]
    return "\n".join(L)


def render_markdown(r: dict) -> str:
    dep = "; ".join(f"{d['type']} at `{d['endpoint']}` ({d['variable']} from `{d['source']}`) - {d['state']}"
                    for d in r["dependencies"]) or "none implicated"
    out = [f"# Incident report {r['id']}", "",
           "| Field | Value |", "|---|---|",
           f"| Incident ID | {r['id']} |",
           f"| Detected at | {hms(r['detected_at'])} |",
           f"| Affected component | **{r['affected_component']['name']}** ({r['affected_component']['status']}) |",
           f"| Failure category | **{r['failure_category_label']}** |",
           f"| Root-cause component | **{_rcc(r)}** |",
           f"| Dependencies involved | {dep} |",
           f"| Also impacted | {', '.join(r['impacted_components']) or '-'} |",
           f"| Confidence | {r['confidence_label']} ({r['confidence']:.0%}) |",
           f"| Investigation status | {r['investigation_status']} |",
           f"| Remediation | {r['remediation']} |", "",
           "## Observed symptoms (facts)"] + [f"- **{s['id']}** {s['text']}" for s in r["observed_symptoms"]]
    out += ["", "## Evidence (observed facts)", "", "| ID | Source | Observation |", "|---|---|---|"]
    out += [f"| {e['id']} | {e['source']} | {e['text'].replace('|', '/')} |" for e in r["evidence"]]
    out += ["", "## Diagnosis (interpretation)"]
    out += [f"{i}. {d['statement']} _[{', '.join(d['facts'])}]_" for i, d in enumerate(r["diagnosis"], 1)]
    out += ["", "## Likely root cause", r["likely_root_cause"]]
    if r["contributing_factors"]:
        out += ["", "## Contributing factors"] + [f"- {c['statement']} _[{', '.join(c['facts'])}]_" for c in r["contributing_factors"]]
    if r.get("correlations"):
        mark = {True: "linked", False: "not linked", None: "not established"}
        out += ["", "## Correlations (checked against order, component and call path)"]
        out += [f"- **{mark[c['linked']]}**: {c['statement']} _[{', '.join(c['facts'])}]_" for c in r["correlations"]]
    out += ["", "## Alternatives considered", "", "| Category | Component | Score | Why not |", "|---|---|---|---|"]
    out += [f"| {a['label']} | {a['component']} | {a['score']:.2f} | {a['why_not'].replace('|', '/')} |"
            for a in r["alternatives_considered"]]
    rec = reconstruction_lines(r.get("reconstruction"))
    if rec:
        out += ["", "## Reconstructed timeline (inferred from the facts)", "```"] + rec[2:] + ["```"]
    out += ["", "## Timeline (observed entries)", ""] + [f"- `{when(t)}` {t['text']}" for t in r["timeline"]]
    if r.get("evidence_coverage"):
        out += ["", "## Evidence coverage and limitations", ""] + [f"- **{c['id']}** {c['text']}" for c in r["evidence_coverage"]]
    out += ["", "## Investigation trace", "```"] + r["investigation_trace"] + ["```", "",
            f"_{r['facts_collected']} facts collected; generated by {r['generated_by']}._", ""]
    return "\n".join(out)


PHASE_LABELS = {"baseline": "Before", "onset": "Onset", "development": "Development", "failure": "Failure",
                "recovery": "Recovery", "post_incident": "After"}


def reconstruction_lines(rc: dict | None) -> list[str]:
    """Phases, answers, ordering and uncertainty. Everything here is inferred and cites the facts it rests on."""
    if not rc:
        return []
    L = ["", "RECONSTRUCTED TIMELINE (inferred from the facts; order only, not cause)"]
    w = rc["window"]
    span = ("not identified" if w["incident_start"] is None else
            f"{hms(w['incident_start'])} - " + (hms(w["incident_end"]) if w["incident_end"] else
                                                "ongoing at collection" if w["ongoing"] else "end not observed"))
    L.append(f"  Window: investigated {hms(w['start'])}-{hms(w['end'])}; dated evidence "
             f"{hms(w['evidence_start'])}-{hms(w['evidence_end'])}; incident {span}")
    for p in rc["phases"]:
        span = ("-" if p["start"] is None else hms(p["start"]) if p["start"] == p["end"] or p["end"] is None
                else f"{hms(p['start'])}-{hms(p['end'])}")
        L.append(f"  {PHASE_LABELS.get(p['phase'], p['phase']):<12} {span:>17}  {p['statement'][:150]}"
                 + (f"  [{', '.join(p['facts'][:6])}]" if p["facts"] else ""))
    if rc.get("changes_after_failure"):
        L.append(f"  {'Afterwards':<12} {'':>17}  {len(rc['changes_after_failure'])} change(s) recorded after the last "
                 f"failure (they came after it, so did not start it)  [{', '.join(c['id'] for c in rc['changes_after_failure'][:6])}]")
    a = rc["answers"]
    L += ["  Questions:"]
    for key, label in (("what_changed_first", "What changed first?"), ("symptoms_began", "When did symptoms begin?"),
                       ("failure_occurred", "When did the failure occur?"),
                       ("immediately_before_failure", "What happened just before it?"),
                       ("after_recovery", "What happened after recovery?")):
        facts = a[key]["facts"]
        L.append(f"    {label:<32} {a[key]['statement'][:150]}" + (f"  [{', '.join(facts[:6])}]" if facts else ""))
    if rc["relations"]:
        L += ["  Ordering:"] + [f"    - {x['statement']}" for x in rc["relations"]]
    if rc["uncertainties"]:
        L += ["  Uncertainty:"] + [f"    - {u}" for u in rc["uncertainties"]]
    return L


def _trim(text: str, limit: int = 2900) -> str:
    return text if len(text) <= limit else text[: limit - 20] + "\n... (truncated)"


def slack_payload(r: dict, report_path: str | None = None) -> dict:
    dep = "\n".join(f"*{d['type']}* `{d['endpoint']}` ({d['variable']}) - {d['state']}" for d in r["dependencies"]) or "none"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Incident {r['id']} - {r['failure_category_label']}"[:150]}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Affected component:*\n{r['affected_component']['name']}"},
            {"type": "mrkdwn", "text": f"*Failure category:*\n{r['failure_category_label']}"},
            {"type": "mrkdwn", "text": f"*Root-cause component:*\n{_rcc(r)}"},
            {"type": "mrkdwn", "text": f"*Confidence:*\n{r['confidence_label']} ({r['confidence']:.0%})"},
            {"type": "mrkdwn", "text": f"*Status:*\n{r['investigation_status']}"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": _trim(f"*Dependencies involved:*\n{dep}")}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _trim(
            "*Observed symptoms:*\n" + "\n".join(f"- {s['text']}" for s in r["observed_symptoms"][:6]))}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _trim(
            "*Diagnosis:*\n" + "\n".join(f"{i}. {d['statement']}" for i, d in enumerate(r["diagnosis"], 1)))}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _trim(f"*Likely root cause:*\n{r['likely_root_cause']}")}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _trim(
            "*Alternatives ruled out:*\n" + "\n".join(f"- {a['label']} ({a['component']}): {a['why_not'][:150]}"
                                                    for a in r["alternatives_considered"][:4]))}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": _trim(
            f"{len(r['evidence'])} evidence facts cited of {r['facts_collected']} collected • {r['remediation']} • "
            f"{r['generated_by']}" + (f" • full report: `{report_path}`" if report_path else ""), 1900)}]},
    ]
    return {"text": f"Incident {r['id']}: {r['failure_category_label']} in {r['affected_component']['name']}", "blocks": blocks}


def detection_payload(incident: dict, namespace: str) -> dict:
    text = (f":large_orange_circle: *Incident detected* in `{namespace}` ({incident['id']})\n"
            + "\n".join(f"- {s['text']}" for s in incident["signals"][:5]) + "\nInvestigating...")
    return {"text": f"Incident detected in {namespace}", "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]}


def resolved_payload(incident: dict, namespace: str) -> dict:
    text = f":large_green_circle: Incident {incident['id']} in `{namespace}`: symptoms have cleared."
    return {"text": text, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]}


def save(r: dict, store: EvidenceStore, dx: dict, reports_dir: Path) -> dict:
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / r["id"]
    paths = {"text": base.with_suffix(".txt"), "markdown": base.with_suffix(".md"), "report": base.with_suffix(".json"),
             "evidence": Path(str(base) + ".evidence.json")}
    paths["text"].write_text(render_text(r), encoding="utf-8")
    paths["markdown"].write_text(render_markdown(r), encoding="utf-8")
    paths["report"].write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")
    paths["evidence"].write_text(json.dumps({"incident_id": r["id"], "window": r["window"], "detected_at": r["detected_at"],
                                             "signals": r["detection_signals"], "store": store.to_dict(), "diagnosis": dx},
                                            indent=1, default=str), encoding="utf-8")
    return {k: str(v) for k, v in paths.items()}
