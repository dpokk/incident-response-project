"""Slack messages for the demo's part of an incident: the Investigation (rule engine + Investigator Agent in one
message, details as a reply), the note on the Remediation message about who acts next, the Remediation Agent's
replies, and the final status. Every value shown comes from the agent's validated report, the deterministic report
and plan, or the execution record; nothing is written here for effect."""
import time

from investigator.slack_view import _context, _header, _section, hms, investigation_details

FIX_TEXT = {"adjust_resource_limit": "Adjust memory limit", "scale_workload": "Scale workload",
            "restore_configuration": "Restore configuration", "rollback_release": "Roll back release",
            "investigate_only": "Investigate further (no change)"}
CHECK = {"pass": ":white_check_mark:", "fail": ":x:"}


def _btn(text: str, action_id: str, value: str, style: str | None = None) -> dict:
    b = {"type": "button", "text": {"type": "plain_text", "text": text}, "action_id": action_id, "value": value}
    return {**b, "style": style} if style else b


def _agent_ok(ai: dict | None) -> bool:
    return (ai or {}).get("status") == "ok"


def _led_by_agent(inc: dict, ai: dict | None) -> bool:
    return (inc.get("route") or {}).get("mode") == "lead" and _agent_ok(ai)


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
        verdict = (f":robot_face: *Led by the AI agent* — the rule engine found no established cause "
                   f"({float(rule.get('confidence') or 0):.0%} confidence) · {ai.get('tool_calls')} checks in "
                   f"{ai.get('seconds')} s")
        confidence, category = f"{r.get('confidence')} (AI agent)", r.get("category")
        evidence = [f["statement"] for f in r["findings"] if f["supported"]][:3]
    else:
        confidence = f"{rule['confidence_label']} ({float(rule.get('confidence') or 0):.0%})"
        category = rule["failure_category_label"]
        evidence = [f"{e['text'][:200]} `{e['id']}`" for e in rule["evidence"][:3]]
        if not _agent_ok(ai):
            verdict = (f":warning: *AI agent unavailable* ({(ai or {}).get('error') or (ai or {}).get('status')}) — "
                       f"the rule engine's findings stand")
        else:
            agree = ai["report"].get("agreement") or {}
            verdict = (f":robot_face: *AI agent check:* :white_check_mark: agrees · {ai.get('tool_calls')} checks in "
                       f"{ai.get('seconds')} s" if agree.get("category") and agree.get("component") else
                       f":robot_face: *AI agent check:* :warning: disagrees — it found "
                       f"{ai['report']['root_cause'][:220]} (`{ai['report']['root_cause_component']}`)")
    blocks = [
        _header(f":mag: Investigation · {inc['id']}"),
        _section(f"*Root cause:* {cause}"),
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Root-cause component*\n{component}"},
            {"type": "mrkdwn", "text": f"*Confidence*\n{confidence}"},
            {"type": "mrkdwn", "text": f"*Category*\n{category}"},
            {"type": "mrkdwn", "text": f"*Also impacted*\n{', '.join(rule['impacted_components']) or 'none observed'}"},
        ]},
    ]
    if evidence:
        blocks.append(_section("*Key evidence*\n" + "\n".join(f"• {e[:240]}" for e in evidence)))
    imp = rule.get("impact") or {}
    if imp.get("assessed"):
        blocks.append(_section(f"*User impact*\n{imp['users']['statement'][:300]}"))
    blocks += [_section(verdict), _context("Details (agent findings, all evidence, timeline) are in the replies.")]
    return {"text": f"Investigation of {inc['id']}: {cause[:150]}", "blocks": blocks}


def details(inc: dict, rule: dict, ai: dict | None) -> dict:
    """The reply under the Investigation: the agent's evidence-checked findings, then the rule engine's details."""
    blocks = []
    if _agent_ok(ai):
        r = ai["report"]
        lines = [f"{':white_check_mark:' if f['supported'] else ':warning:'} {f['statement'][:260]}  "
                 f"_({', '.join(e['id'] for e in f['evidence']) or 'no valid evidence'})_" for f in r["findings"][:6]]
        if r.get("summary"):
            lines.insert(0, r["summary"][:500])
        if lines:
            blocks.append(_section("*AI agent findings (evidence-checked)*\n" + "\n".join(lines)))
        blocks.append(_context(f"{ai.get('tool_calls')} tool calls in {ai.get('seconds')} s · model `{ai.get('model')}`"
                               + ("" if r["validation"]["ok"] else
                                  " · validation: " + "; ".join(r["validation"]["problems"][:2]))))
    blocks += investigation_details(rule)["blocks"]
    return {"text": f"Investigation details for {inc['id']}", "blocks": blocks[:50]}


def plan_note(proposal: dict, gate: dict | None) -> str:
    """One line on the Remediation message: who acts next, and why."""
    par = proposal.get("parameter") or {}
    if proposal.get("executable") and (gate or {}).get("eligible"):
        return (":robot_face: *Automatic:* the policy gate passed, so the Remediation Agent applies this fix. Its "
                "attempts follow as replies; press *Stop* there to take over.")
    if proposal.get("executable"):
        failed = next((c["detail"] for c in (gate or {}).get("checks", []) if not c["ok"]), None)
        return (":bust_in_silhouette: *Needs an engineer*" + (f" — not automated: {failed}" if failed else "")
                + (f"\n:bulb: The AI agent suggests `{par['value']}`" if par.get("value") else "")
                + ("\n:bulb: This fix comes from the AI agent's evidence-checked report (the rule engine had none)."
                   if proposal.get("led_by") == "agent" and proposal.get("source") == "agent" else ""))
    return f":no_entry: *No automatic fix:* {proposal.get('reason')}. The manual steps are in the final status message."


# --------------------------------------------------------------------------- final status (its own message)

def _final(inc: dict, title: str, lines: list[str], extra: list[dict] | None = None) -> dict:
    now = time.time()
    took = now - inc["detected_at"] if inc.get("detected_at") else None
    lines = [x for x in lines if x]
    if took is not None:
        lines.append(f"*Duration:* detected {hms(inc['detected_at'])} → {hms(now)} "
                     f"({int(took // 60)} min {int(took % 60)} s)")
    return {"text": f"{inc['id']}: {title}", "blocks": [_header(f"{title} · {inc['id']}"),
                                                       _section("\n".join(lines))] + (extra or [])}


def _cause(inc: dict) -> str | None:
    return f"*Root cause:* {inc['cause'][:300]}" if inc.get("cause") else None


def _verified(v: dict) -> str | None:
    crit = (v or {}).get("criteria") or []
    if not crit:
        return None
    ok = sum(1 for c in crit if c["status"] == "pass")
    return f"*Verified:* {ok}/{len(crit)} checks held — " + " · ".join(c["statement"] for c in crit[:4])


def _change(rec: dict) -> str | None:
    a = (rec or {}).get("applied") or {}
    return f"`{a.get('before')}` → `{a.get('after')}`" if a.get("accepted") else None


def resolved(inc: dict, record: dict) -> dict:
    by = f"<@{record['executor']}>" if record.get("executor") else "an engineer"
    return _final(inc, ":white_check_mark: Resolved", [
        _cause(inc), f"*Fix:* {inc.get('fix') or 'approved change'} {_change(record) or ''} — executed by {by}",
        _verified(record.get("verification"))])


def not_resolved(inc: dict, record: dict) -> dict:
    v = record.get("verification") or {}
    rb = v.get("rollback") or {}
    failed = [c["statement"] for c in v.get("criteria") or [] if c["status"] != "pass"]
    return _final(inc, ":x: Not resolved", [
        _cause(inc), f"*Fix tried:* {inc.get('fix') or 'approved change'} {_change(record) or ''}",
        f"*Outcome:* {record.get('outcome')} — {(v.get('reason') or '')[:300]}",
        ("*Checks that did not hold:* " + " · ".join(failed[:4])) if failed else None,
        "*Next:* an engineer must decide. " + ("A rollback plan is in the Remediation replies (it needs its own "
                                              "Approve + Execute)." if rb.get("available") else
                                              "No further change was made.")])


def failed(inc: dict, record: dict) -> dict:
    return _final(inc, ":warning: Remediation stopped", [
        _cause(inc), f"*Fix:* {inc.get('fix') or 'approved change'}",
        f"*Why it stopped:* {(record.get('message') or record.get('status') or '')[:400]}",
        "*Next:* nothing was retried. An engineer must check the system and fix it manually."])


def rejected(inc: dict, decision: dict) -> dict:
    what = "rejected" if decision["decision"] == "rejected" else "sent back for more investigation"
    return _final(inc, ":no_entry: Fix not applied", [
        _cause(inc), f"*Proposed fix:* {inc.get('fix') or '-'} — {what} by <@{decision['reviewer']}>",
        "*Next:* no change was made; the engineer owns the incident."])


def manual(inc: dict, proposal: dict) -> dict:
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(proposal.get("manual_steps") or [], 1))
    return _final(inc, ":construction_worker: Handed to engineer", [
        _cause(inc), f"*Why no automatic fix:* {proposal.get('reason')}",
        f"*Manual steps (from the AI agent):*\n{steps}" if steps else "*Manual steps:* none suggested",
        "*Next:* press *Acknowledge* on the Remediation message when you take it over."])


def acknowledged(user: str, components: list | None = None) -> dict:
    return {"text": "Acknowledged", "blocks": [_section(
        f":ballot_box_with_check: <@{user}> acknowledged and is fixing it manually."
        + (f" Automatic remediation is re-enabled for {', '.join('`' + c + '`' for c in components)}."
           if components else ""))]}


# --------------------------------------------------------------------------- the Remediation Agent

def auto_start(inc: dict, ladder: list, proposal: dict, gate: dict) -> dict:
    steps = " → ".join(f"`{v}`" for v in ladder)
    return {"text": f"Remediation Agent started on {inc['id']}", "blocks": [
        _section(f":robot_face: *Remediation Agent started* — {proposal['summary']}\n*Values to try:* {steps}"
                 + (" (the next only if the previous did not resolve it)" if len(ladder) > 1 else "")
                 + "\nIf none works, every change is reverted and an engineer takes over."),
        {"type": "actions", "elements": [_btn("Stop automatic remediation", "auto:stop", inc["id"], "danger")]}]}


def auto_attempt(inc: dict, att: dict) -> dict:
    rec = att.get("record") or {}
    head = f":gear: *Attempt {att['n']} of {att['of']}* — value `{att['value']}`"
    stage = att.get("stage")
    if stage == "approved":
        line = "applying with the standard safety checks…"
    elif stage == "checks_passed":
        line = "safety checks passed; dry run and change follow…"
    elif stage == "applied":
        a = rec.get("applied") or {}
        line = f"applied `{a.get('before')}` → `{a.get('after')}`; verifying…"
    elif stage == "done" and att.get("outcome"):
        v = att.get("verification") or {}
        crit = " · ".join(f"{CHECK.get(c['status'], ':grey_question:')} {c['statement']}" for c in v.get("criteria", [])[:4])
        line = f"*{att['outcome']}*" + (f" ({att.get('before')} → {att.get('after')})" if att.get("after") else "") \
            + (f"\n{crit}" if crit else "")
    else:
        line = f":no_entry: stopped: {(att.get('message') or rec.get('message') or '')[:300]}"
    return {"text": f"Remediation Agent attempt {att['n']}: {att.get('outcome') or stage}",
            "blocks": [_section(f"{head} — {line}")]}


def auto_revert(inc: dict, info: dict) -> dict:
    if info.get("stage") == "started":
        text = f":rewind: *Reverting* `{info.get('from')}` → `{info.get('to')}` (no attempt resolved it)…"
    elif info.get("ok"):
        text = f":rewind: *Reverted* `{info.get('before')}` → `{info.get('after')}`."
    else:
        text = f":warning: *Revert not completed:* {(info.get('message') or '')[:400]}"
    return {"text": "Remediation Agent revert", "blocks": [_section(text)]}


def auto_resolved(inc: dict, out: dict) -> dict:
    a = out["resolved_by"]
    return _final(inc, ":white_check_mark: Resolved automatically", [
        _cause(inc), f"*Fix:* {inc.get('fix') or 'change'} `{a.get('before')}` → `{a.get('after')}` — by the "
                     f"Remediation Agent (attempt {a['n']} of {a['of']}, no human click)",
        _verified(a.get("verification"))])


def auto_handover(inc: dict, out: dict) -> dict:
    tried = ", ".join(f"`{a['value']}` → {a.get('outcome') or a.get('code')}" for a in out["attempts"]) or "none"
    rv = out.get("revert")
    extra = []
    if not out.get("stopped_by"):
        extra = [{"type": "actions", "elements": [_btn("Acknowledge — I will fix it manually", "auto:ack", inc["id"],
                                                       "primary")]}]
    return _final(inc, ":construction_worker: Handed to engineer", [
        _cause(inc), f"*Automatic remediation did not work:* {out.get('reason')}", f"*Tried:* {tried}",
        (f"*Reverted:* `{rv.get('before')}` → `{rv.get('after')}`" if rv.get("ok") else
         f"*Revert not completed:* {(rv.get('message') or '')[:200]}") if rv else None,
        f"*Owner:* <@{out['stopped_by']}>" if out.get("stopped_by") else
        f"*Next:* `{out.get('component')}` is locked against automation until an engineer presses *Acknowledge*."],
        extra)


def auto_stopped(inc: dict, user: str) -> dict:
    return _final(inc, ":octagonal_sign: Taken over by an engineer", [
        _cause(inc), f"<@{user}> pressed *Stop*: the current step finishes; no further attempt and no revert.",
        f"*Owner:* <@{user}>"])
