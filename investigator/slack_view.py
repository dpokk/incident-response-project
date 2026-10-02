"""Slack presentation (Iteration 6): renders the existing structured report and RemediationPlan as Block Kit.

Presentation only. This module does not determine root causes, choose or change remediation, query any system or
execute anything: every word it shows comes from the report dict, the RemediationPlan dict (the Iteration 5
contract) and the review status it is given. The same objects remain usable by a web UI or an API.

Interactive controls carry only identifiers (incident, plan digest, action index); the decision itself is made and
recorded by review.py, never here.
"""
import json
from datetime import datetime

STALE_AFTER_S = 15 * 60                  # default only: callers pass the execution policy's max plan age
ROLLBACK_MARK = "#rollback-"             # review key of a rollback plan: <incident>#rollback-<execution>
OUTCOME_ICON = {"RESOLVED": ":white_check_mark:", "NOT_RESOLVED": ":x:", "INCONCLUSIVE": ":grey_question:"}
EXEC_TEXT = {"claimed": ":gear: executing — safety checks running", "applying": ":gear: executing — applying the change",
             "verifying": ":mag: change applied — verifying", "refused": ":no_entry: execution refused",
             "apply_failed": ":no_entry: the change was rejected by the cluster", "uncertain": ":warning: execution "
             "state uncertain — not retried; fresh investigation required"}
CHECK_MARK = {True: ":white_check_mark:", False: ":x:", None: ":grey_question:"}
CHANGE_TYPES = {"adjust_resource_limit", "scale_workload", "restore_configuration", "rollback_release"}
STATUS_ICON = {"awaiting_review": ":hourglass_flowing_sand:", "approved": ":white_check_mark:", "rejected": ":x:",
               "investigate_first": ":mag:", "acknowledged": ":ballot_box_with_check:", "superseded": ":no_entry_sign:"}
STATUS_TEXT = {"awaiting_review": "awaiting review", "approved": "approved (decision recorded, not executed)",
               "rejected": "rejected", "investigate_first": "investigate first (no change approved)",
               "acknowledged": "acknowledged", "superseded": "superseded by a newer plan: this decision no longer applies"}
TYPE_TEXT = {"adjust_resource_limit": "Adjust resource limit", "scale_workload": "Scale workload",
             "restore_configuration": "Restore configuration", "rollback_release": "Roll back release",
             "investigate_further": "Investigate further"}
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


def _age(collected_at: float | None, now: float, stale_after_s: float = STALE_AFTER_S) -> tuple[str, bool]:
    if not collected_at:
        return "collection time unknown", True
    age = max(0.0, now - collected_at)
    span = f"{age / 3600:.1f} h" if age >= 3600 else f"{age / 60:.0f} min" if age >= 60 else f"{age:.0f} s"
    # Slack renders the date token in the viewer's time zone and keeps "{ago}" current whenever the message is
    # viewed; the text after "|" is the fallback for clients that cannot render it (fixed at render time).
    token = f"<!date^{int(collected_at)}^{{time_secs}} ({{ago}})|{hms(collected_at)} ({span} ago)>"
    return f"evidence collected at {token}", age > stale_after_s


# --------------------------------------------------------------------------- thread messages

def thread_root(incident_id: str, namespace: str, signals: list[str] | None = None, headline: str | None = None,
                review_summary: str | None = None, resolved: bool = False, execution_summary: str | None = None) -> dict:
    """The thread's first message: what was detected, and (once known) the diagnosis and review status."""
    icon = ":large_green_circle:" if resolved else ":large_orange_circle:"
    lines = [f"{icon} *Incident {incident_id}* in `{namespace}`" + (" — symptoms have cleared" if resolved else "")]
    if headline:
        lines.append(f"*Diagnosis:* {headline}")
    else:
        lines += [f"• {s[:200]}" for s in (signals or [])[:5]] + ([] if resolved else ["_Investigating…_"])
    if review_summary:
        lines.append(f"*Remediation review:* {review_summary}")
    if execution_summary:
        lines.append(f"*Remediation:* {execution_summary}")
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
                 interactive: bool = True, stale_after_s: float = STALE_AFTER_S, executable: bool = False,
                 executions: dict[int, dict] | None = None) -> dict:
    """The RemediationPlan for review. `statuses` maps action index -> {"status", "reviewer", "at",
    "supplied_parameters"} as recorded by the review layer; `executions` maps action index -> the execution record
    (Iteration 7). `executable`: an executor is available, so an approved change action offers Execute.
    `stale_after_s` comes from the execution policy (max plan age). This function only displays what it is given."""
    age, stale = _age(collected_at, now, stale_after_s)
    rollback = ROLLBACK_MARK in str(plan.get("incident_id", ""))
    title = "Rollback plan — proposed, not executed" if rollback else "Remediation plan — proposed, not executed"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": title}},
        _section(f"*{ASSESSMENT_TEXT.get(plan['assessment'], plan['assessment'])}* — {plan['assessment_reason']}\n"
                 f"*Incident state:* {plan['incident_state']} (when the evidence was collected)"),
    ]
    if stale:
        blocks.append(_section(f":warning: *This plan reflects the state observed at {hms(collected_at)}.* The system may "
                               f"have changed since. It is older than the {stale_after_s / 60:.0f} min the execution "
                               f"policy allows: Execute will be refused; run a fresh investigation."))
    if plan["current_state"]:
        blocks.append(_section("*Current state (at collection):*\n" + "\n".join(
            f"• {c['statement'][:220]}{_cite(c['fact_ids'])}" for c in plan["current_state"][:6])))
    for i, a in enumerate(plan["actions"]):
        blocks += _action_blocks(i, a, plan, digest, statuses.get(i, {"status": "awaiting_review"}), interactive,
                                 executable and not stale, (executions or {}).get(i))
    u = plan["uncertainty"]
    unc = f"*Confidence:* {u['confidence_label']} ({u['confidence']:.0%}, from the diagnosis)"
    if u["competing_causes"]:
        unc += "\n*Competing explanations:* " + "; ".join(f"{c['category']} in {c['component']} ({c['score']:.2f})"
                                                        for c in u["competing_causes"])
    if u["evidence_gaps"]:
        unc += "\n*Evidence gaps:* " + "; ".join(g[:160] for g in u["evidence_gaps"][:3])
    blocks += [{"type": "divider"}, _section(unc),
               _context(f":lock: Requires human approval. Approving records a decision only — nothing is executed until "
                        f"an authorised person clicks Execute, which re-checks the live state, applies the policy and "
                        f"dry-runs first. • {age} • plan `{digest[:12]}`")]
    return {"text": f"Remediation plan for {plan['incident_id']}: {ASSESSMENT_TEXT.get(plan['assessment'])}",
            "blocks": blocks[:50]}


def _action_blocks(i: int, a: dict, plan: dict, digest: str, st: dict, interactive: bool, executable: bool = False,
                   execution: dict | None = None) -> list[dict]:
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
        detail += ["*Verification (evaluated after execution):*"] + [f"• {v['statement']}" for v in a["verification"]]
        out.append(_section("\n".join(detail)))
    out.append(_context(f"{STATUS_ICON.get(status, '')} *{STATUS_TEXT.get(status, status)}*" + (
        f" — by <@{st['reviewer']}> at {hms(st['at'])}" if st.get("reviewer") else "") + (
        "; supplied: " + ", ".join(f"{k} = `{v}`" for k, v in st["supplied_parameters"].items())
        if st.get("supplied_parameters") else "")))
    if execution:
        out.append(_context(execution_status_line(execution)))
    elif interactive and executable and status == "approved" and a["type"] in CHANGE_TYPES:
        value = json.dumps({"incident": plan["incident_id"], "digest": digest, "action": i}, separators=(",", ":"))
        out.append({"type": "actions", "block_id": f"exec:{i}", "elements": [
            _button("Execute", f"exec:{i}", value, "danger")]})
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


# --------------------------------------------------------------------------- execution (Iteration 7)

def execution_status_line(rec: dict) -> str:
    """One line under an action in the plan message, from its execution record."""
    who = f" — executed by <@{rec['executor']}>" if rec.get("executor") else ""
    if rec.get("status") == "completed" and rec.get("outcome"):
        return f"{OUTCOME_ICON.get(rec['outcome'], '')} *executed — {rec['outcome']}*{who}"
    return f"*{EXEC_TEXT.get(rec.get('status'), rec.get('status'))}*{who}" + (
        f": {rec['message'][:200]}" if rec.get("status") in ("refused", "apply_failed", "uncertain") and rec.get("message")
        else "")


def _checks_text(checks: list[dict]) -> str:
    return "\n".join(f"{CHECK_MARK.get(c.get('ok'))} {c.get('detail', c.get('check'))[:220]}" for c in checks[:14])


def execution_message(plan: dict, index: int, executor: str, stage: str, checks: list[dict] | None = None,
                      record: dict | None = None, window: dict | None = None) -> dict:
    """The evolving message for ONE execution: safety checks -> executing -> verifying -> outcome. Shows what the
    execution record and the executor report; decides nothing."""
    a = plan["actions"][index]
    rec = record or {}
    target = a["target"].get("component") or a["target"].get("config_item")
    title = {"checks": "Execution checks", "applied": "Executing", "verifying": "Verifying", "observing": "Verifying",
             "completed": "Execution result", "stopped": "Execution stopped"}.get(stage, "Execution")
    lines = [f"*{index + 1}. {TYPE_TEXT.get(a['type'], a['type'])}* · target `{target}`",
             f"Requested by <@{executor}>" + (f" · approved by <@{rec['approver']}>" if rec.get("approver") else "")
             + (f" · plan `{rec['plan_digest'][:12]}`" if rec.get("plan_digest") else "")]
    if rec.get("change"):
        lines.append(f"*Change:* {_describe(a['type'], rec['change'])}")
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"{title} — action {index + 1}"}},
              _section("\n".join(lines))]
    if checks:
        blocks.append(_section("*Safety checks:*\n" + _checks_text(checks)))
    dry = rec.get("dry_run")
    if dry:
        blocks.append(_section(f"*Dry run:* {CHECK_MARK[bool(dry.get('accepted'))]} "
                               + ("accepted by the cluster (nothing persisted)" if dry.get("accepted")
                                  else f"rejected — {dry.get('error')}")))
    applied = rec.get("applied")
    if applied and applied.get("accepted"):
        ops = "\n".join(f"• `{d[:200]}`" for d in applied.get("detail") or [])
        blocks.append(_section(f"*Applied at {hms(applied.get('at'))}:* `{applied.get('before')}` → "
                               f"`{applied.get('after')}`\n{ops}"))
    v = rec.get("verification")
    if v and v.get("criteria") is not None and rec.get("outcome"):
        crit = "\n".join(f"{CHECK_MARK.get({'pass': True, 'fail': False}.get(c['status']))} {c['statement'][:120]} — "
                         f"_{(c.get('evidence') or ['no evidence'])[-1][:160]}_" for c in v["criteria"][:8])
        w = v.get("window") or {}
        span = (f" (settled after {(v['settle']['ended_at'] - v['settle']['started_at']):.0f} s, then observed "
                f"{(w.get('ended_at', 0) - w.get('started_at', 0)):.0f} s)" if v.get("settle") and w else "")
        blocks.append(_section(f"*Verification{span}:*\n{crit or '_no criteria_'}"))
        blocks.append(_section(f"{OUTCOME_ICON.get(rec['outcome'], '')} *Outcome: {rec['outcome']}* — "
                               f"{v.get('reason', '')[:400]}"))
        rb = v.get("rollback")
        if rec["outcome"] != "RESOLVED":
            blocks.append(_context("No further change was made." + (
                " A rollback plan has been posted below for review: it needs its own approval and Execute."
                if rb and rb.get("available") else (f" No rollback is available ({rb['reason']})." if rb else ""))))
    elif stage in ("applied", "verifying", "observing") and window:
        blocks.append(_context(f":hourglass_flowing_sand: Verifying with the plan's criteria: waiting up to "
                               f"{window.get('settle_max_s', 0):.0f} s for the change to settle, then observing for "
                               f"{window.get('window_s', 0):.0f} s. Nothing is reported as resolved before that."))
    if rec.get("status") in ("refused", "apply_failed", "uncertain"):
        blocks.append(_section(f":no_entry: *{EXEC_TEXT.get(rec['status'])}:* {rec.get('message', '')[:500]}"))
    return {"text": f"{title} — action {index + 1}: {rec.get('outcome') or rec.get('status') or stage}",
            "blocks": blocks[:50]}


def execution_refused_message(plan: dict | None, index: int, executor: str, message: str,
                              checks: list[dict] | None = None) -> dict:
    """A request refused before anything was claimed (stale plan, superseded, not approved, policy, ...)."""
    a = (plan or {}).get("actions", [{}] * (index + 1))[index] if plan else {}
    head = f":no_entry: *Execution refused* — action {index + 1}" + (
        f" ({TYPE_TEXT.get(a.get('type'), a.get('type'))})" if a else "") + f", requested by <@{executor}>"
    blocks = [_section(f"{head}\n{message}")]
    if checks:
        blocks.append(_context(_checks_text(checks)))
    return {"text": f"Execution refused: {message[:150]}", "blocks": blocks}


def _describe(atype: str, c: dict) -> str:
    if atype == "adjust_resource_limit":
        return (f"memory limit of `{c.get('component')}` `{_mi(c.get('expected_bytes'))}` → "
                f"`{_mi(c.get('target_bytes'))}`")
    if atype == "scale_workload":
        return f"replicas of `{c.get('component')}` `{c.get('expected_replicas')}` → `{c.get('target_replicas')}`"
    if atype == "rollback_release":
        return f"image of `{c.get('component')}` `{c.get('expected_image')}` → `{c.get('target_image')}`"
    return (f"host in `{c.get('item')}` (`{c.get('source')}`) `{c.get('expected_host')}` → "
            f"`{c.get('target_host') or 'recorded previous value'}`, then restart `{c.get('component')}`")


def _mi(b) -> str:
    return f"{b / 2**20:g}Mi" if b else "?"
