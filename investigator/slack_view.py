"""Slack presentation (Iteration 6): renders the existing structured report and RemediationPlan as Block Kit.

Presentation only. This module does not determine root causes, choose or change remediation, query any system or
execute anything: every word it shows comes from the report dict, the RemediationPlan dict (the Iteration 5
contract) and the review status it is given. The same objects remain usable by a web UI or an API.

Interactive controls carry only identifiers (incident, plan digest, action index); the decision itself is made and
recorded by review.py, never here.
"""
import json
from datetime import datetime

STALE_AFTER_S = 15 * 60                  # older than this, the plan is flagged as possibly out of date
CHANGE_TYPES = {"adjust_resource_limit", "scale_workload", "restore_configuration"}
STATUS_ICON = {"awaiting_review": ":hourglass_flowing_sand:", "approved": ":white_check_mark:", "rejected": ":x:",
               "investigate_first": ":mag:", "acknowledged": ":ballot_box_with_check:", "superseded": ":no_entry_sign:"}
STATUS_TEXT = {"awaiting_review": "awaiting review", "approved": "approved (decision recorded, not executed)",
               "rejected": "rejected", "investigate_first": "investigate first (no change approved)",
               "acknowledged": "acknowledged", "superseded": "superseded by a newer plan: this decision no longer applies"}
TYPE_TEXT = {"adjust_resource_limit": "Adjust resource limit", "scale_workload": "Scale workload",
             "restore_configuration": "Restore configuration", "investigate_further": "Investigate further"}
ASSESSMENT_TEXT = {"action_proposed": "Action proposed", "no_immediate_action": "No immediate action needed",
                   "investigate_further": "Investigate further", "no_safe_action": "No safe action available"}
PARAMETER_HINT = {"adjust_resource_limit": ("New memory limit", "e.g. 256Mi or 1Gi"),
                  "scale_workload": ("Replicas to restore", "whole number, e.g. 1"),
                  "restore_configuration": ("Host to restore", "hostname, e.g. postgres")}


def hms(t) -> str:
    return datetime.fromtimestamp(t).astimezone().strftime("%H:%M:%S") if t else "?"


def _trim(text: str, limit: int = 2900) -> str:
    return text if len(text) <= limit else text[: limit - 20] + "\n... (truncated)"


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _trim(text)}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _trim(text, 1900)}]}


def _cite(ids) -> str:
    return f" `{', '.join(ids[:4])}`" if ids else ""


def _age(collected_at: float | None, now: float) -> tuple[str, bool]:
    if not collected_at:
        return "collection time unknown", True
    age = max(0.0, now - collected_at)
    span = f"{age / 3600:.1f} h" if age >= 3600 else f"{age / 60:.0f} min" if age >= 60 else f"{age:.0f} s"
    # Slack renders the date token in the viewer's time zone and keeps "{ago}" current whenever the message is
    # viewed; the text after "|" is the fallback for clients that cannot render it (fixed at render time).
    token = f"<!date^{int(collected_at)}^{{time_secs}} ({{ago}})|{hms(collected_at)} ({span} ago)>"
    return f"evidence collected at {token}", age > STALE_AFTER_S


# --------------------------------------------------------------------------- thread messages

def thread_root(incident_id: str, namespace: str, signals: list[str] | None = None, headline: str | None = None,
                review_summary: str | None = None, resolved: bool = False) -> dict:
    """The thread's first message: what was detected, and (once known) the diagnosis and review status."""
    icon = ":large_green_circle:" if resolved else ":large_orange_circle:"
    lines = [f"{icon} *Incident {incident_id}* in `{namespace}`" + (" — symptoms have cleared" if resolved else "")]
    if headline:
        lines.append(f"*Diagnosis:* {headline}")
    else:
        lines += [f"• {s[:200]}" for s in (signals or [])[:5]] + ([] if resolved else ["_Investigating…_"])
    if review_summary:
        lines.append(f"*Remediation review:* {review_summary}")
    text = "\n".join(lines)
    return {"text": f"Incident {incident_id} in {namespace}", "blocks": [_section(text)]}


def investigation_message(r: dict) -> dict:
    """Diagnosis, root cause, components, confidence, evidence, impact and the key timeline answers."""
    rcc = r.get("root_cause_component")
    rcc_text = f"{rcc['name']} ({rcc['relation']})" if rcc else "not determined"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Investigation — {r['failure_category_label']}"[:150]}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Affected component:*\n{r['affected_component']['name']}"},
            {"type": "mrkdwn", "text": f"*Root-cause component:*\n{rcc_text}"},
            {"type": "mrkdwn", "text": f"*Impacted:*\n{', '.join(r['impacted_components']) or 'none observed'}"},
            {"type": "mrkdwn", "text": f"*Confidence:*\n{r['confidence_label']} ({r['confidence']:.0%})"},
        ]},
        _section(f"*Likely root cause:*\n{r['likely_root_cause']}"),
        _section("*Evidence:*\n" + "\n".join(f"• `{e['id']}` {e['text'][:220]}" for e in r["evidence"][:6])),
    ]
    imp = r.get("impact") or {}
    if imp.get("assessed"):
        blocks.append(_section("*Impact:*\n" + "\n".join(f"• {x}" for x in [
            imp["instances"]["statement"], imp["users"]["statement"], "Duration: " + imp["duration"]["statement"],
            "Propagation: " + imp["propagation_statement"]])))
    answers = ((r.get("reconstruction") or {}).get("answers") or {})
    if answers:
        blocks.append(_section("*Timeline:*\n" + "\n".join(
            f"• {answers[k]['statement'][:240]}" for k in ("what_changed_first", "failure_occurred", "after_recovery")
            if k in answers)))
    alts = [a for a in r.get("alternatives_considered", []) if a.get("score", 0) >= 0.3][:3]
    if alts:
        blocks.append(_context("Also considered: " + "; ".join(f"{a['label']} in {a['component']} ({a['score']:.2f})"
                                                                for a in alts)))
    blocks.append(_context(f"{r['facts_collected']} facts collected • {r['generated_by']}"))
    return {"text": f"Investigation of {r['id']}: {r['failure_category_label']} in {r['affected_component']['name']}",
            "blocks": blocks}


def plan_message(plan: dict, digest: str, statuses: dict[int, dict], collected_at: float | None, now: float,
                 interactive: bool = True) -> dict:
    """The RemediationPlan for review. `statuses` maps action index -> {"status", "reviewer", "at",
    "supplied_parameters"} as recorded by the review layer; this function only displays them."""
    age, stale = _age(collected_at, now)
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Remediation plan — proposed, not executed"}},
        _section(f"*{ASSESSMENT_TEXT.get(plan['assessment'], plan['assessment'])}* — {plan['assessment_reason']}\n"
                 f"*Incident state:* {plan['incident_state']} (when the evidence was collected)"),
    ]
    if stale:
        blocks.append(_section(f":warning: *This plan reflects the state observed at {hms(collected_at)}.* The system may "
                               f"have changed since; the state will be re-checked before anything is executed "
                               f"(a later milestone)."))
    if plan["current_state"]:
        blocks.append(_section("*Current state (at collection):*\n" + "\n".join(
            f"• {c['statement'][:220]}{_cite(c['fact_ids'])}" for c in plan["current_state"][:6])))
    for i, a in enumerate(plan["actions"]):
        blocks += _action_blocks(i, a, plan, digest, statuses.get(i, {"status": "awaiting_review"}), interactive)
    u = plan["uncertainty"]
    unc = f"*Confidence:* {u['confidence_label']} ({u['confidence']:.0%}, from the diagnosis)"
    if u["competing_causes"]:
        unc += "\n*Competing explanations:* " + "; ".join(f"{c['category']} in {c['component']} ({c['score']:.2f})"
                                                        for c in u["competing_causes"])
    if u["evidence_gaps"]:
        unc += "\n*Evidence gaps:* " + "; ".join(g[:160] for g in u["evidence_gaps"][:3])
    blocks += [{"type": "divider"}, _section(unc),
               _context(f":lock: Requires human approval. Approving records a decision only — nothing is executed by "
                        f"this system yet. • {age} • plan `{digest[:12]}`")]
    return {"text": f"Remediation plan for {plan['incident_id']}: {ASSESSMENT_TEXT.get(plan['assessment'])}",
            "blocks": blocks[:50]}


def _action_blocks(i: int, a: dict, plan: dict, digest: str, st: dict, interactive: bool) -> list[dict]:
    status = st.get("status", "awaiting_review")
    target = a["target"].get("component") or a["target"].get("config_item")
    head = (f"*{i + 1}. {TYPE_TEXT.get(a['type'], a['type'])}* · {a['urgency']} · target `{target}`\n{a['summary']}")
    out = [{"type": "divider"}, _section(head)]
    params = {k: v for k, v in a["parameters"].items() if k != "questions" and v is not None}
    if a["type"] == "investigate_further":
        out.append(_section("*Questions:*\n" + "\n".join(f"• {s['statement'][:240]}{_cite(s['fact_ids'])}"
                                                         for s in a["rationale"][:6])))
    else:
        detail = ["*Why:*"] + [f"• {s['statement'][:240]}{_cite(s['fact_ids'])}" for s in a["rationale"][:4]]
        if params:
            detail.append("*Parameters:* " + ", ".join(f"{k} = `{v}`" for k, v in params.items()))
        if not a["parameters_complete"]:
            detail.append(":memo: *Needs your input:* " + "; ".join(a["preconditions"]))
        detail += ["*Expected final state:*"] + [f"• {x}" for x in a["expected_final_state"]]
        detail += ["*Risks:*"] + [f"• {s['statement'][:240]}{_cite(s['fact_ids'])}" for s in a["risks"][:4]]
        if a["rollback"]:
            detail.append(f"*Rollback:* {a['rollback']['statement']}")
        detail += ["*Verification (after execution, later milestone):*"] + [f"• {v['statement']}" for v in a["verification"]]
        out.append(_section("\n".join(detail)))
    out.append(_context(f"{STATUS_ICON.get(status, '')} *{STATUS_TEXT.get(status, status)}*" + (
        f" — by <@{st['reviewer']}> at {hms(st['at'])}" if st.get("reviewer") else "") + (
        "; supplied: " + ", ".join(f"{k} = `{v}`" for k, v in st["supplied_parameters"].items())
        if st.get("supplied_parameters") else "")))
    if interactive and status == "awaiting_review":
        value = json.dumps({"incident": plan["incident_id"], "digest": digest, "action": i}, separators=(",", ":"))
        if a["type"] in CHANGE_TYPES:
            if not a["parameters_complete"]:
                label, hint = PARAMETER_HINT.get(a["type"], ("Value", ""))
                out.append({"type": "input", "block_id": f"param:{i}", "optional": True,
                            "label": {"type": "plain_text", "text": label},
                            "hint": {"type": "plain_text", "text": "Required to approve. The evidence does not supply "
                                                                   "this value; a candidate is only a suggestion."},
                            "element": {"type": "plain_text_input", "action_id": "value",
                                        "placeholder": {"type": "plain_text", "text": hint}}})
            out.append({"type": "actions", "block_id": f"review:{i}", "elements": [
                _button("Approve", f"review:approved:{i}", value, "primary"),
                _button("Reject", f"review:rejected:{i}", value, "danger"),
                _button("Investigate first", f"review:investigate_first:{i}", value)]})
        else:
            out.append({"type": "actions", "block_id": f"review:{i}", "elements": [
                _button("Acknowledge", f"review:acknowledged:{i}", value)]})
    return out


def _button(text: str, action_id: str, value: str, style: str | None = None) -> dict:
    b = {"type": "button", "text": {"type": "plain_text", "text": text}, "action_id": action_id, "value": value}
    if style:
        b["style"] = style
    return b


def decision_message(record: dict, plan: dict) -> dict:
    """A thread reply announcing a recorded decision."""
    a = plan["actions"][record["action_index"]]
    supplied = record.get("supplied_parameters") or {}
    text = (f"{STATUS_ICON.get(record['decision'], '')} <@{record['reviewer']}> recorded *{record['decision']}* for action "
            f"{record['action_index'] + 1} ({TYPE_TEXT.get(a['type'], a['type'])}: {a['summary'][:160]})"
            + (f" with " + ", ".join(f"{k} = `{v}`" for k, v in supplied.items()) if supplied else "")
            + f".\nDecision recorded against plan `{record['plan_digest'][:12]}`. Nothing has been executed.")
    return {"text": f"Decision recorded: {record['decision']}", "blocks": [_section(text)]}


def notice(text: str) -> dict:
    return {"text": text, "blocks": [_section(text)]}
