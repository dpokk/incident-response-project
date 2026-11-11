"""Slack messages for the demo's part of an incident: the Investigation (rule engine and Investigator Agent in one
message, details as a reply), the Reviewer Agent's reply, the note on the Remediation message about who acts next,
the Remediation Agent's replies, and the final status. Every value shown comes from the agent's validated report, the
deterministic report and plan, or the execution record; nothing is written here for effect."""
import re
import time

from investigator.slack_view import _context, _header, _section, hms, investigation_details

FIX_TEXT = {"adjust_resource_limit": "Adjust memory limit", "scale_workload": "Scale workload",
            "restore_configuration": "Restore configuration", "rollback_release": "Roll back release",
            "investigate_only": "Investigate further (no change)"}
CHECK = {"pass": ":white_check_mark:", "fail": ":x:"}

# Why the Remediation Agent did not act, in plain words, most meaningful first
GATE_REASON = [
    ("enabled", "automatic remediation is switched off"),
    ("action_allowed", "this kind of change always needs an engineer"),
    ("reviewer_agrees", "the Reviewer Agent challenged the diagnosis"),
    ("not_locked", "an earlier automatic fix on this component failed and is not acknowledged yet"),
    ("known_pattern", "the rule engine was not confident, so the AI agent led"),
    ("rule_confidence", None),                       # the gate's own text carries the percentage
    ("agent_report", "the AI agent produced no report"),
    ("agent_agrees", "the AI agent and the rule engine do not agree"),
    ("evidence_checked", "not every finding is backed by collected evidence"),
    ("value", "there is no validated value to apply"),
]


def _btn(text: str, action_id: str, value: str, style: str | None = None) -> dict:
    b = {"type": "button", "text": {"type": "plain_text", "text": text}, "action_id": action_id, "value": value}
    return {**b, "style": style} if style else b


def _agent_ok(ai: dict | None) -> bool:
    return (ai or {}).get("status") == "ok"


def _led_by_agent(inc: dict, ai: dict | None) -> bool:
    return (inc.get("route") or {}).get("mode") == "lead" and _agent_ok(ai)


def _secs(s) -> str:
    return f"{float(s):.0f} s" if s is not None else "?"


def headline(inc: dict, rule: dict, ai: dict | None) -> tuple[str, str]:
    """(root cause, root-cause component) the incident is summarised by: the agent's when it led, else the rules'."""
    if _led_by_agent(inc, ai):
        r = ai["report"]
        return r["root_cause"], r["root_cause_component"]
    return rule["likely_root_cause"], (rule.get("root_cause_component") or {}).get("name") or "not determined"


# --------------------------------------------------------------------------- investigation

def investigation(inc: dict, rule: dict, ai: dict | None) -> dict:
    cause, component = headline(inc, rule, ai)
    if _led_by_agent(inc, ai):
        r = ai["report"]
        verdict = (f":robot_face: *Led by the AI agent.* The rule engine found no clear cause "
                   f"({float(rule.get('confidence') or 0):.0%} confidence). {ai.get('tool_calls')} checks in "
                   f"{_secs(ai.get('seconds'))}.")
        confidence, category = f"{str(r.get('confidence')).capitalize()} (AI agent)", \
            str(r.get("category") or "").replace("_", " ").capitalize()
        evidence = [f["statement"] for f in r["findings"] if f["supported"]][:3]
    else:
        confidence = f"{rule['confidence_label']} ({float(rule.get('confidence') or 0):.0%})"
        category = rule["failure_category_label"]
        evidence = [f"{e['text'][:180]} `{e['id']}`" for e in rule["evidence"][:3]]
        if not _agent_ok(ai):
            verdict = (f":warning: *AI agent unavailable* ({(ai or {}).get('error') or (ai or {}).get('status')}). "
                       f"The rule engine's findings stand.")
        else:
            agree = ai["report"].get("agreement") or {}
            verdict = (f":robot_face: *AI check:* agrees. {ai.get('tool_calls')} checks in {_secs(ai.get('seconds'))}."
                       if agree.get("category") and agree.get("component") else
                       f":robot_face: *AI check:* it found a different cause: "
                       f"{ai['report']['root_cause'][:200]} (`{ai['report']['root_cause_component']}`)")
    blocks = [
        _header(f":mag: Investigation: {inc['id']}"),
        _section(f"*Root cause:* {cause}"),
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Component*\n{component}"},
            {"type": "mrkdwn", "text": f"*Confidence*\n{confidence}"},
            {"type": "mrkdwn", "text": f"*Category*\n{category}"},
            {"type": "mrkdwn", "text": f"*Also affected*\n{', '.join(rule['impacted_components']) or 'none seen'}"},
        ]},
    ]
    if evidence:
        blocks.append(_section("*Key evidence*\n" + "\n".join(f"• {e[:220]}" for e in evidence)))
    imp = rule.get("impact") or {}
    if imp.get("assessed"):
        blocks.append(_section(f"*User impact:* {imp['users']['statement'][:260]}"))
    blocks.append(_section(verdict))
    return {"text": f"Investigation of {inc['id']}: {cause[:150]}", "blocks": blocks}


def details(inc: dict, rule: dict, ai: dict | None) -> dict:
    """The reply under the Investigation: the agent's findings, then the rule engine's details."""
    blocks = []
    if _agent_ok(ai):
        r = ai["report"]
        lines = [f"{':white_check_mark:' if f['supported'] else ':warning:'} {f['statement'][:220]} "
                 f"`{', '.join(e['id'] for e in f['evidence']) or 'no evidence'}`" for f in r["findings"][:4]]
        if r.get("summary"):
            lines.insert(0, r["summary"][:400])
        if lines:
            blocks.append(_section("*AI agent findings*\n" + "\n".join(lines)))
        blocks.append(_context(f"AI agent: {ai.get('tool_calls')} checks in {_secs(ai.get('seconds'))}"
                               + ("" if r["validation"]["ok"] else
                                  ". Note: " + "; ".join(r["validation"]["problems"][:2]))))
    blocks += investigation_details(rule, shown=0 if _led_by_agent(inc, ai) else 3)["blocks"]
    return {"text": f"Investigation details for {inc['id']}", "blocks": blocks[:50]}


REVIEW_HEAD = {"agree": ":white_check_mark: *Reviewer Agent agrees.*",
               "challenge": ":warning: *Reviewer Agent challenges the diagnosis.* Automatic remediation is off.",
               "reject": ":no_entry: *Reviewer Agent rejects the diagnosis.* The AI agent's fix is not used and "
                         "automatic remediation is off.",
               "unsupported": ":grey_question: *Reviewer Agent objected without valid evidence,* so the objection is "
                              "ignored."}


def review(inc: dict, rv: dict) -> dict:
    """The reply under the Investigation: the Reviewer Agent's verdict and its evidence-checked objections."""
    if rv.get("status") != "ok":
        return {"text": f"Reviewer Agent: no review for {inc['id']}", "blocks": [_section(
            f":warning: *Reviewer Agent unavailable* ({rv.get('error') or rv.get('status')}). The incident continues "
            f"without a review.")]}
    r = rv["review"]
    lines = [REVIEW_HEAD[r["effective"]]]
    if r.get("summary"):
        lines.append(r["summary"][:400])
    lines += [f"{':small_red_triangle:' if o['supported'] else ':grey_question:'} {o['statement'][:220]} "
              f"`{', '.join(e['id'] for e in o['evidence']) or 'no evidence'}`" for o in r["objections"][:3]]
    return {"text": f"Reviewer Agent: {r['effective']} ({inc['id']})", "blocks": [
        _section("\n".join(lines)),
        _context(f"Reviewer: {rv.get('tool_calls')} checks in {_secs(rv.get('seconds'))}")]}


def gate_reason(gate: dict | None) -> str | None:
    """The most meaningful reason the Remediation Agent did not act, in plain words."""
    failed = {c["check"]: c["detail"] for c in (gate or {}).get("checks", []) if not c["ok"]}
    for check, text in GATE_REASON:
        if check in failed:
            return text or failed[check]
    return next(iter(failed.values()), None)


def plan_note(proposal: dict, gate: dict | None) -> str:
    """One line on the Remediation message: who acts next, and why."""
    par = proposal.get("parameter") or {}
    if proposal.get("executable") and (gate or {}).get("eligible"):
        return (":robot_face: *Automatic.* All the safety conditions hold, so the Remediation Agent applies this fix. "
                "Its attempts follow as replies. Press *Stop* there to take over.")
    if proposal.get("executable"):
        why = gate_reason(gate)
        return (":bust_in_silhouette: *Needs an engineer*" + (f" because {why}." if why else ".")
                + (f"\n:bulb: The AI agent suggests `{par['value']}`." if par.get("value") else "")
                + ("\n:bulb: This fix comes from the AI agent's report; the rule engine had none."
                   if proposal.get("led_by") == "agent" and proposal.get("source") == "agent" else ""))
    reason = str(proposal.get("reason") or "").rstrip(".")
    return f":no_entry: *No automatic fix.* {reason + '. ' if reason else ''}The manual steps are in the final message."


# --------------------------------------------------------------------------- final status (its own message)

def _final(inc: dict, title: str, lines: list[str], extra: list[dict] | None = None) -> dict:
    now = time.time()
    took = now - inc["detected_at"] if inc.get("detected_at") else None
    lines = [x for x in lines if x]
    if took is not None:
        lines.append(f"*Duration:* {int(took // 60)} min {int(took % 60)} s ({hms(inc['detected_at'])} to {hms(now)})")
    return {"text": f"{inc['id']}: {title}", "blocks": [_header(f"{title}: {inc['id']}"),
                                                       _section("\n".join(lines))] + (extra or [])}


def _cause(inc: dict) -> str | None:
    return f"*Root cause:* {inc['cause'][:260]}" if inc.get("cause") else None


def _verified(v: dict) -> str | None:
    crit = (v or {}).get("criteria") or []
    if not crit:
        return None
    ok = sum(1 for c in crit if c["status"] == "pass")
    return f"*Verified:* {ok} of {len(crit)} checks held."


def _change(rec: dict) -> str | None:
    a = (rec or {}).get("applied") or {}
    return f"from `{a.get('before')}` to `{a.get('after')}`" if a.get("accepted") else None


def _short(text: str | None, default: str = "approved change") -> str:
    """The plan's fix summary up to its first qualifier, e.g. "Raise backend's memory limit"."""
    return re.split(r"\s*[;(]", text or "")[0].strip() or default


def _fix(inc: dict, record: dict) -> str:
    return " ".join(x for x in (_short(inc.get("fix")), _change(record)) if x)


def resolved(inc: dict, record: dict) -> dict:
    by = f"<@{record['executor']}>" if record.get("executor") else "an engineer"
    return _final(inc, ":white_check_mark: Resolved", [
        _cause(inc), f"*Fix:* {_fix(inc, record)}, executed by {by}.",
        _verified(record.get("verification"))])


def not_resolved(inc: dict, record: dict) -> dict:
    v = record.get("verification") or {}
    rb = v.get("rollback") or {}
    failed = [c["statement"] for c in v.get("criteria") or [] if c["status"] != "pass"]
    return _final(inc, ":x: Not resolved", [
        _cause(inc), f"*Fix tried:* {_fix(inc, record)}.",
        f"*Outcome:* {record.get('outcome')}.",
        ("*Checks that failed:* " + "; ".join(failed[:4]) + ".") if failed else None,
        "*Next:* an engineer decides. " + ("A rollback plan is in the Remediation replies. It needs its own Approve "
                                           "and Execute." if rb.get("available") else "No further change was made.")])


def failed(inc: dict, record: dict) -> dict:
    return _final(inc, ":warning: Remediation stopped", [
        _cause(inc), f"*Fix:* {_short(inc.get('fix'))}",
        f"*Why it stopped:* {(record.get('message') or record.get('status') or '')[:300]}",
        "*Next:* nothing was retried. An engineer checks the system and fixes it by hand."])


def rejected(inc: dict, decision: dict) -> dict:
    what = "rejected" if decision["decision"] == "rejected" else "sent back for more investigation"
    return _final(inc, ":no_entry: Fix not applied", [
        _cause(inc), f"*Proposed fix:* {_short(inc.get('fix'), 'none')}, {what} by <@{decision['reviewer']}>.",
        "*Next:* no change was made. The engineer owns the incident."])


def manual(inc: dict, proposal: dict) -> dict:
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(proposal.get("manual_steps") or [], 1))
    return _final(inc, ":construction_worker: Handed to an engineer", [
        _cause(inc), f"*Why no automatic fix:* {proposal.get('reason')}.",
        f"*Suggested steps:*\n{steps}" if steps else "*Suggested steps:* none.",
        "*Next:* press *Acknowledge* on the Remediation message when you take it over."])


def acknowledged(user: str, components: list | None = None) -> dict:
    return {"text": "Acknowledged", "blocks": [_section(
        f":ballot_box_with_check: <@{user}> acknowledged and is fixing it by hand."
        + (f" Automatic remediation is enabled again for {', '.join('`' + c + '`' for c in components)}."
           if components else ""))]}


# --------------------------------------------------------------------------- the Remediation Agent

def auto_start(inc: dict, ladder: list, proposal: dict, gate: dict) -> dict:
    steps = ", then ".join(f"`{v}`" for v in ladder)
    return {"text": f"Remediation Agent started on {inc['id']}", "blocks": [
        _section(f":robot_face: *Remediation Agent started:* {_short(proposal['summary'])}.\n*Values to try:* {steps}"
                 + (" (each one only if the previous did not fix it)" if len(ladder) > 1 else "")
                 + "\nIf none works, every change is reverted and an engineer takes over."),
        {"type": "actions", "elements": [_btn("Stop automatic remediation", "auto:stop", inc["id"], "danger")]}]}


def auto_attempt(inc: dict, att: dict) -> dict:
    rec = att.get("record") or {}
    head = f":gear: *Attempt {att['n']} of {att['of']}* with `{att['value']}`:"
    stage = att.get("stage")
    if stage == "approved":
        line = "running the safety checks."
    elif stage == "checks_passed":
        line = "safety checks passed, applying the change."
    elif stage == "applied":
        a = rec.get("applied") or {}
        line = f"changed from `{a.get('before')}` to `{a.get('after')}`, verifying."
    elif stage == "done" and att.get("outcome"):
        v = att.get("verification") or {}
        crit = "\n".join(f"{CHECK.get(c['status'], ':grey_question:')} {c['statement']}" for c in v.get("criteria", [])[:4])
        line = f"*{att['outcome']}*." + (f"\n{crit}" if crit else "")
    else:
        line = f"stopped. {(att.get('message') or rec.get('message') or '')[:260]}"
    return {"text": f"Remediation Agent attempt {att['n']}: {att.get('outcome') or stage}",
            "blocks": [_section(f"{head} {line}")]}


def auto_revert(inc: dict, info: dict) -> dict:
    if info.get("stage") == "started":
        text = f":rewind: *Reverting* from `{info.get('from')}` to `{info.get('to')}`, because no attempt fixed it."
    elif info.get("ok"):
        text = f":rewind: *Reverted* from `{info.get('before')}` to `{info.get('after')}`."
    else:
        text = f":warning: *Revert not completed.* {(info.get('message') or '')[:300]}"
    return {"text": "Remediation Agent revert", "blocks": [_section(text)]}


def auto_resolved(inc: dict, out: dict) -> dict:
    a = out["resolved_by"]
    return _final(inc, ":white_check_mark: Resolved automatically", [
        _cause(inc), f"*Fix:* {_short(inc.get('fix'))} from `{a.get('before')}` to `{a.get('after')}`, applied by "
                     f"the Remediation Agent on attempt {a['n']} of {a['of']}.",
        _verified(a.get("verification"))])


def auto_handover(inc: dict, out: dict) -> dict:
    tried = ", ".join(f"`{a['value']}` ({a.get('outcome') or a.get('code')})" for a in out["attempts"]) or "none"
    rv = out.get("revert")
    extra = []
    if not out.get("stopped_by"):
        extra = [{"type": "actions", "elements": [_btn("Acknowledge: I will fix it by hand", "auto:ack", inc["id"],
                                                       "primary")]}]
    return _final(inc, ":construction_worker: Handed to an engineer", [
        _cause(inc), f"*Automatic remediation did not fix it:* {out.get('reason')}", f"*Tried:* {tried}",
        (f"*Reverted:* from `{rv.get('before')}` to `{rv.get('after')}`" if rv.get("ok") else
         f"*Revert not completed:* {(rv.get('message') or '')[:200]}") if rv else None,
        f"*Owner:* <@{out['stopped_by']}>" if out.get("stopped_by") else
        f"*Next:* automation is paused for `{out.get('component')}` until an engineer presses *Acknowledge*."],
        extra)


def auto_stopped(inc: dict, user: str) -> dict:
    return _final(inc, ":octagonal_sign: Taken over by an engineer", [
        _cause(inc), f"<@{user}> pressed *Stop*. The current step finishes; there is no further attempt and no revert.",
        f"*Owner:* <@{user}>"])
