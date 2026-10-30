"""Slack presentation (Iteration 6): renders the existing structured report and RemediationPlan as Block Kit.

Presentation only. This module does not determine root causes, choose or change remediation, query any system or
execute anything: every word it shows comes from the report dict, the RemediationPlan dict (the Iteration 5
contract) and the review status it is given. The same objects remain usable by a web UI or an API.

Each incident is a few headed channel messages, each holding only what a reader needs at that step:
  Incident detected (status kept current) · Investigation (+ a reply with the details) · Remediation (buttons;
  decisions and execution progress are replies to it) · the final status (posted by the caller, e.g. the demo).

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
STATUS_TEXT = {"awaiting_review": "awaiting review", "approved": "approved",
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


def _header(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150], "emoji": True}}


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


# --------------------------------------------------------------------------- incident detected (kept current)

def thread_root(incident_id: str, namespace: str, signals: list[str] | None = None, headline: str | None = None,
                review_summary: str | None = None, resolved: bool = False, execution_summary: str | None = None,
                status: str | None = None) -> dict:
    """The incident's first message: what was detected, and one status line that is re-rendered as the incident
    progresses. `status` (when the caller tracks one) replaces the status derived from the other fields."""
    parts = [f"*Diagnosis:* {headline}"] if headline else []
    if status:
        parts.append(f"*Status:* {status}")
    else:
        if review_summary:
            parts.append(f"*Review:* {review_summary}")
        if execution_summary:
            parts.append(f"*Remediation:* {execution_summary}")
        if not headline and not resolved:
            parts.append("_Investigating…_")
    if resolved:
        parts.append(":large_green_circle: symptoms have cleared")
    return {"text": f"Incident {incident_id} in {namespace}", "blocks": [
        _header(f":rotating_light: Incident detected · {incident_id}"),
        _section("\n".join(f"• {s[:200]}" for s in (signals or [])[:3]) or "_no signal recorded_"),
        _context(f"`{namespace}` · " + " · ".join(parts))]}


# --------------------------------------------------------------------------- investigation

def _rcc_text(r: dict) -> str:
    rcc = r.get("root_cause_component")
    return f"{rcc['name']}" if rcc else "not determined"


def investigation_message(r: dict) -> dict:
    """The diagnosis at a glance: what failed, why, how sure, the strongest evidence and who is affected."""
    blocks = [
        _header(f":mag: Investigation · {r['id']}"),
        _section(f"*{r['failure_category_label']}* in `{r['affected_component']['name']}`\n"
                 f"*Root cause:* {r['likely_root_cause']}"),
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Root-cause component*\n{_rcc_text(r)}"},
            {"type": "mrkdwn", "text": f"*Confidence*\n{r['confidence_label']} ({r['confidence']:.0%})"},
            {"type": "mrkdwn", "text": f"*Affected*\n{r['affected_component']['name']}"},
            {"type": "mrkdwn", "text": f"*Also impacted*\n{', '.join(r['impacted_components']) or 'none observed'}"},
        ]},
    ]
    if r["evidence"]:
        blocks.append(_section("*Key evidence*\n" + "\n".join(f"• {e['text'][:200]} `{e['id']}`"
                                                             for e in r["evidence"][:3])))
    users = (((r.get("impact") or {}).get("users") or {}).get("statement")) if (r.get("impact") or {}).get("assessed") \
        else None
    if users:
        blocks.append(_section(f"*User impact*\n{users[:300]}"))
    blocks.append(_context("Details (all evidence, impact, timeline) are in the replies."))
    return {"text": f"Investigation of {r['id']}: {r['failure_category_label']} in {r['affected_component']['name']}",
            "blocks": blocks}


def investigation_details(r: dict) -> dict:
    """A reply to the Investigation message: everything behind the summary, for whoever wants to check it."""
    blocks = [_section("*All evidence*\n" + ("\n".join(f"• `{e['id']}` {e['text'][:220]}" for e in r["evidence"][:8])
                                            or "_none_"))]
    imp = r.get("impact") or {}
    if imp.get("assessed"):
        blocks.append(_section("*Impact*\n" + "\n".join(f"• {x}" for x in [
            imp["instances"]["statement"], imp["users"]["statement"], "Duration: " + imp["duration"]["statement"],
            "Propagation: " + imp["propagation_statement"]])))
    answers = ((r.get("reconstruction") or {}).get("answers") or {})
    timeline = [answers[k]["statement"][:240] for k in ("what_changed_first", "failure_occurred", "after_recovery")
                if k in answers]
    if timeline:
        blocks.append(_section("*Timeline*\n" + "\n".join(f"• {x}" for x in timeline)))
    alts = [a for a in r.get("alternatives_considered", []) if a.get("score", 0) >= 0.3][:3]
    if alts:
        blocks.append(_context("Also considered: " + "; ".join(f"{a['label']} in {a['component']} ({a['score']:.2f})"
                                                                for a in alts)))
    blocks.append(_context(f"{r['facts_collected']} facts collected · {r['generated_by']}"))
    return {"text": f"Investigation details for {r['id']}", "blocks": blocks[:50]}


# --------------------------------------------------------------------------- remediation

def _target(a: dict) -> str:
    return a["target"].get("component") or a["target"].get("config_item")


def _change_line(a: dict, supplied: dict | None = None) -> str | None:
    """`before` → `after` for a change action, from its parameters (or the value a reviewer supplied)."""
    p, chosen = a["parameters"], next(iter((supplied or {}).values()), None)
    if a["type"] == "adjust_resource_limit":
        before, after = p.get("current_limit"), chosen or p.get("proposed_limit")
        what = f"{p.get('resource', 'memory')} limit of `{_target(a)}`"
    elif a["type"] == "scale_workload":
        before, after = p.get("current_replicas"), chosen or p.get("target_replicas")
        what = f"replicas of `{_target(a)}`"
    elif a["type"] == "restore_configuration":
        before = p.get("current_host")
        after = chosen or p.get("restore_to_host") or ("its previous value" if p.get("restore_to") else None)
        what = f"host in `{_target(a)}`"
    elif a["type"] == "rollback_release":
        before, after = p.get("current_image"), chosen or p.get("previous_image")
        what = f"image of `{_target(a)}`"
    else:
        return None
    after_text = (f"`{after}`" if after not in (None, "its previous value") else
                  after or "_the value you enter_")
    return f"{what}: `{before}` → {after_text}"


def plan_message(plan: dict, digest: str, statuses: dict[int, dict], collected_at: float | None, now: float,
                 interactive: bool = True, stale_after_s: float = STALE_AFTER_S, executable: bool = False,
                 executions: dict[int, dict] | None = None, note: str | None = None) -> dict:
    """The RemediationPlan for review. `statuses` maps action index -> {"status", "reviewer", "at",
    "supplied_parameters"} as recorded by the review layer; `executions` maps action index -> the execution record
    (Iteration 7). `executable`: an executor is available, so an approved change action offers Execute.
    `stale_after_s` comes from the execution policy (max plan age). `note`: one line from the caller about who acts
    next (e.g. the demo's automation gate). This function only displays what it is given."""
    age, stale = _age(collected_at, now, stale_after_s)
    rollback = ROLLBACK_MARK in str(plan.get("incident_id", ""))
    incident = str(plan.get("incident_id", "")).split(ROLLBACK_MARK)[0]
    blocks = [_header(f":leftwards_arrow_with_hook: Rollback plan · {incident}" if rollback
                      else f":hammer_and_wrench: Remediation · {incident}")]
    if plan["assessment"] != "action_proposed":
        blocks.append(_section(f"*{ASSESSMENT_TEXT.get(plan['assessment'], plan['assessment'])}* — "
                               f"{plan['assessment_reason']}"))
    if note:
        blocks.append(_section(note))
    if stale:
        blocks.append(_section(f":warning: *This plan reflects the state observed at {hms(collected_at)}.* It is older "
                               f"than the {stale_after_s / 60:.0f} min the execution policy allows: Execute will be "
                               f"refused; run a fresh investigation."))
    for i, a in enumerate(plan["actions"]):
        blocks += _action_blocks(i, a, plan, digest, statuses.get(i, {"status": "awaiting_review"}), interactive,
                                 executable and not stale, (executions or {}).get(i))
    u = plan["uncertainty"]
    doubts = []
    if u["competing_causes"]:
        doubts.append("*Competing explanations:* " + "; ".join(f"{c['category']} in {c['component']} ({c['score']:.2f})"
                                                              for c in u["competing_causes"]))
    if u["evidence_gaps"]:
        doubts.append("*Evidence gaps:* " + "; ".join(g[:160] for g in u["evidence_gaps"][:2]))
    if doubts:
        blocks.append(_context("\n".join(doubts)))
    blocks.append(_context(f"{age} · plan `{digest[:12]}`"))
    return {"text": f"Remediation plan for {plan['incident_id']}: {ASSESSMENT_TEXT.get(plan['assessment'])}",
            "blocks": blocks[:50]}


def _action_blocks(i: int, a: dict, plan: dict, digest: str, st: dict, interactive: bool, executable: bool = False,
                   execution: dict | None = None) -> list[dict]:
    status = st.get("status", "awaiting_review")
    many = len(plan["actions"]) > 1
    label = f"{i + 1}. " if many else ""
    out = [{"type": "divider"}] if many else []
    if a["type"] == "investigate_further":
        out.append(_section(f"*{label}Open questions* (no change to the system)\n" + "\n".join(
            f"• {s['statement'][:240]}{_cite(s['fact_ids'])}" for s in a["rationale"][:3])))
    else:
        lines = [f"*{label}Fix:* {a['summary']}"]
        change = _change_line(a, st.get("supplied_parameters"))
        if change:
            lines.append(f"*Change:* {change}")
        if not a["parameters_complete"] and not st.get("supplied_parameters"):
            lines.append(":memo: *Needs your input:* " + "; ".join(a["preconditions"]))
        if a["risks"]:
            lines.append(f"*Risk:* {a['risks'][0]['statement'][:220]}")
        if a["verification"]:
            lines.append("*Success check:* " + " · ".join(v["statement"] for v in a["verification"]))
        if a["rollback"]:
            lines.append(f"*Undo:* {a['rollback']['statement']}")
        out.append(_section("\n".join(lines)))
    out.append(_context(_status_line(a, st, executable, execution)))
    if not execution and interactive and executable and status == "approved" and a["type"] in CHANGE_TYPES:
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


def _status_line(a: dict, st: dict, executable: bool, execution: dict | None) -> str:
    """Where this action stands and what happens next."""
    if execution:
        return execution_status_line(execution)
    status = st.get("status", "awaiting_review")
    who = f" by <@{st['reviewer']}> at {hms(st['at'])}" if st.get("reviewer") else ""
    line = f"{STATUS_ICON.get(status, '')} *{STATUS_TEXT.get(status, status)}*{who}"
    if status == "awaiting_review":
        line += (" — next: *Approve*, then *Execute*" if a["type"] in CHANGE_TYPES else
                 " — *Acknowledge* when you take it over")
    elif status == "approved" and a["type"] in CHANGE_TYPES and executable:
        line += " — next: *Execute*"
    return line


def _button(text: str, action_id: str, value: str, style: str | None = None) -> dict:
    b = {"type": "button", "text": {"type": "plain_text", "text": text}, "action_id": action_id, "value": value}
    if style:
        b["style"] = style
    return b


def decision_message(record: dict, plan: dict) -> dict:
    """A reply to the Remediation message announcing a recorded decision."""
    a = plan["actions"][record["action_index"]]
    supplied = record.get("supplied_parameters") or {}
    text = (f"{STATUS_ICON.get(record['decision'], '')} *{STATUS_TEXT.get(record['decision'], record['decision'])}* "
            f"by <@{record['reviewer']}>"
            + (" with " + ", ".join(f"`{v}`" for v in supplied.values()) if supplied else "")
            + (" — next: *Execute*" if record["decision"] == "approved" and a["type"] in CHANGE_TYPES else ""))
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
    """All passed: one line. Otherwise only the checks that did not pass, with why."""
    failed = [c for c in checks if c.get("ok") is not True]
    if not failed:
        return f":white_check_mark: all {len(checks)} passed"
    return "\n".join(f"{CHECK_MARK.get(c.get('ok'))} {c.get('detail', c.get('check'))[:220]}" for c in failed[:8])


def execution_message(plan: dict, index: int, executor: str, stage: str, checks: list[dict] | None = None,
                      record: dict | None = None, window: dict | None = None) -> dict:
    """The evolving reply for ONE execution: safety checks -> executing -> verifying -> outcome. Shows what the
    execution record and the executor report; decides nothing."""
    a = plan["actions"][index]
    rec = record or {}
    title = {"checks": "Execution checks", "applied": "Executing", "verifying": "Verifying", "observing": "Verifying",
             "completed": "Execution result", "stopped": "Execution stopped"}.get(stage, "Execution")
    lines = [f"*{title}* — {TYPE_TEXT.get(a['type'], a['type'])} `{_target(a)}` · by <@{executor}>"
             + (f" · approved by <@{rec['approver']}>" if rec.get("approver") else "")]
    if rec.get("change"):
        lines.append(f"*Change:* {_describe(a['type'], rec['change'])}")
    if checks:
        lines.append(f"*Safety checks:* {_checks_text(checks)}")
    dry = rec.get("dry_run")
    if dry:
        lines.append(f"*Dry run:* {CHECK_MARK[bool(dry.get('accepted'))]} "
                     + ("accepted" if dry.get("accepted") else f"rejected — {dry.get('error')}"))
    applied = rec.get("applied")
    if applied and applied.get("accepted"):
        lines.append(f"*Applied at {hms(applied.get('at'))}:* `{applied.get('before')}` → `{applied.get('after')}`")
    blocks = [_section("\n".join(lines))]
    v = rec.get("verification")
    if v and v.get("criteria") is not None and rec.get("outcome"):
        crit = "\n".join(f"{CHECK_MARK.get({'pass': True, 'fail': False}.get(c['status']))} {c['statement'][:120]}"
                         + ("" if c["status"] == "pass" else f" — _{(c.get('evidence') or ['no evidence'])[-1][:160]}_")
                         for c in v["criteria"][:8])
        w = v.get("window") or {}
        span = (f" (settled after {(v['settle']['ended_at'] - v['settle']['started_at']):.0f} s, then observed "
                f"{(w.get('ended_at', 0) - w.get('started_at', 0)):.0f} s)" if v.get("settle") and w else "")
        blocks.append(_section(f"*Verification{span}:*\n{crit or '_no criteria_'}\n"
                               f"{OUTCOME_ICON.get(rec['outcome'], '')} *Outcome: {rec['outcome']}* — "
                               f"{v.get('reason', '')[:400]}"))
        rb = v.get("rollback")
        if rec["outcome"] != "RESOLVED":
            blocks.append(_context("No further change was made." + (
                " A rollback plan has been posted below for review: it needs its own approval and Execute."
                if rb and rb.get("available") else (f" No rollback is available ({rb['reason']})." if rb else ""))))
    elif stage in ("applied", "verifying", "observing") and window:
        blocks.append(_context(f":hourglass_flowing_sand: Verifying: up to {window.get('settle_max_s', 0):.0f} s to "
                               f"settle, then {window.get('window_s', 0):.0f} s of observation."))
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
