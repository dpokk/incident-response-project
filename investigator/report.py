"""Structured incident report: one format for every failure category.

Observed facts (what the system showed) are kept separate from the diagnosis (what we conclude),
and every diagnostic statement cites the fact IDs it rests on.
"""
import json
from datetime import datetime
from pathlib import Path

from .evidence import EvidenceStore

TIMELINE_KINDS = {"detection_signal", "process_terminated", "event", "log_signature", "log_exception",
                  "change", "config_changed", "metric_traffic_change", "metric_error_ratio", "metric_memory_high",
                  "log_tail_before_exit"}


def hms(t) -> str:
    return datetime.fromtimestamp(t).astimezone().strftime("%H:%M:%S") if t else "-"


def build(incident: dict, store: EvidenceStore, dx: dict, window: tuple, ongoing: bool | None) -> dict:
    facts = {f.id: f for f in store.facts}

    def fx(ids):
        return [{"id": i, "source": facts[i].source, "text": facts[i].text, "t": facts[i].t} for i in ids if i in facts]

    ws = next((w for w in dx["component_summary"] if w["component"] == dx["affected_component"]), {})
    status_fact = next(iter(store.find(kind="component_status", subject=f"component/{dx['affected_component']}")), None)
    timeline = sorted((f for f in store.facts if f.t and f.kind in TIMELINE_KINDS and window[0] - 60 <= f.t <= window[1] + 5),
                      key=lambda f: f.t)
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
        "dependencies": dx["dependencies"],
        "impacted_components": dx["impacted_components"],
        "observed_symptoms": fx(dx["symptoms"]),
        "evidence": fx(dx["evidence"]),
        "diagnosis": [{"statement": r["statement"], "facts": r["facts"]} for r in dx["reasoning"]],
        "likely_root_cause": dx["root_cause"],
        "contributing_factors": dx["contributing_factors"],
        "confidence": dx["confidence"],
        "confidence_label": dx["confidence_label"],
        "alternatives_considered": dx["alternatives"],
        "timeline": [{"t": f.t, "id": f.id, "text": f.text} for f in _dedupe(timeline)][:30],
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
         f"Failure category:     {r['failure_category_label']}"]
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
    L += ["", f"CONFIDENCE: {r['confidence_label']} ({r['confidence']:.0%})", "",
          "ALTERNATIVES CONSIDERED"]
    L += [f"  - {a['label']} in {a['component']} (score {a['score']:.2f}): {a['why_not']}" for a in r["alternatives_considered"]]
    L += ["", "TIMELINE"] + [f"  {hms(t['t'])}  {t['text'][:150]}" for t in r["timeline"]]
    L += ["", f"Investigation: {r['facts_collected']} facts collected by {sum(1 for x in r['investigation_trace'] if x.startswith('  '))} "
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
    out += ["", "## Alternatives considered", "", "| Category | Component | Score | Why not |", "|---|---|---|---|"]
    out += [f"| {a['label']} | {a['component']} | {a['score']:.2f} | {a['why_not'].replace('|', '/')} |"
            for a in r["alternatives_considered"]]
    out += ["", "## Timeline", ""] + [f"- `{hms(t['t'])}` {t['text']}" for t in r["timeline"]]
    out += ["", "## Investigation trace", "```"] + r["investigation_trace"] + ["```", "",
            f"_{r['facts_collected']} facts collected; generated by {r['generated_by']}._", ""]
    return "\n".join(out)


def _trim(text: str, limit: int = 2900) -> str:
    return text if len(text) <= limit else text[: limit - 20] + "\n... (truncated)"


def slack_payload(r: dict, report_path: str | None = None) -> dict:
    dep = "\n".join(f"*{d['type']}* `{d['endpoint']}` ({d['variable']}) - {d['state']}" for d in r["dependencies"]) or "none"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Incident {r['id']} - {r['failure_category_label']}"[:150]}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Affected component:*\n{r['affected_component']['name']}"},
            {"type": "mrkdwn", "text": f"*Failure category:*\n{r['failure_category_label']}"},
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
