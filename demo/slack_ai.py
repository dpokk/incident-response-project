"""Slack messages for the Investigator Agent's part of the incident thread. Every value shown comes from the agent's
validated report, the deterministic plan, or the execution record; nothing is written here for effect."""
from investigator.slack_view import _context, _section

FIX_TEXT = {"adjust_resource_limit": "Adjust memory limit", "scale_workload": "Scale workload",
            "restore_configuration": "Restore configuration", "investigate_only": "Investigate further (no change)"}


def started(inc: dict, model: str | None) -> dict:
    text = (f":mag: *Investigator Agent* started investigating *{inc['id']}*.\n"
            f"First the deterministic rule engine collects evidence; then the agent "
            + (f"(`{model}`) " if model else "(no model configured: rule-based findings only) ")
            + "investigates with read-only tools.")
    return {"text": f"Investigator Agent started on {inc['id']}", "blocks": [_section(text)]}


def report(inc: dict, ai: dict, proposal: dict, rule: dict, gate: dict | None = None) -> dict:
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"Investigator Agent report — {inc['id']}"}}]
    rt = inc.get("route") or {}
    if rt:
        blocks.append(_context(
            f":compass: *{'Known pattern — the rule engine led; the agent verified' if rt['mode'] == 'verify' else 'Unfamiliar for the rules — the Investigator Agent led'}* "
            f"({rt['reason']})"))
    if (ai or {}).get("status") != "ok":
        blocks.append(_section(f":warning: The agent produced no report ({(ai or {}).get('error') or (ai or {}).get('status')}). "
                               f"The rule-based findings below stand: *{rule.get('failure_category_label')}* in "
                               f"`{(rule.get('affected_component') or {}).get('name')}`."))
    else:
        r = ai["report"]
        agree = r.get("agreement") or {}
        blocks.append(_section(f"*Root cause* ({r.get('confidence')} confidence): {r['root_cause']}\n"
                               f"*Component:* `{r['root_cause_component']}` · *category:* `{r.get('category')}`"))
        if r.get("summary"):
            blocks.append(_section(r["summary"]))
        lines = []
        for f in r["findings"][:6]:
            mark = ":white_check_mark:" if f["supported"] else ":warning:"
            ids = ", ".join(e["id"] for e in f["evidence"]) or "no valid evidence"
            lines.append(f"{mark} {f['statement'][:260]}  _({ids})_")
        if lines:
            blocks.append(_section("*Findings (evidence-checked):*\n" + "\n".join(lines)))
        fix = r["suggested_fix"]
        blocks.append(_section(f"*Suggested fix:* {FIX_TEXT.get(fix['action'], fix['action'])} → `{fix['target']}`"
                               + (f" = `{fix['parameter_value']}`" if fix.get("parameter_value") else "")
                               + (f"\n{fix['description'][:300]}" if fix.get("description") else "")))
        blocks.append(_context(
            ("Agrees with the rule engine" if agree.get("category") and agree.get("component") else
             f"Rule engine said: {agree.get('rule_label')} in {agree.get('rule_component')}")
            + f" · {ai.get('tool_calls')} tool calls in {ai.get('seconds')} s · model `{ai.get('model')}`"
            + ("" if r["validation"]["ok"] else " · validation: " + "; ".join(r["validation"]["problems"][:2]))))
    if proposal.get("executable") and (gate or {}).get("eligible"):
        par = proposal.get("parameter") or {}
        blocks.append(_section(":robot_face: *Policy gate passed: the Remediation Agent executes this automatically* "
                               f"(no human click needed). Proposed action: {proposal['summary']}"
                               + (f" — {par['name']} = `{par['value']}`" if par.get("value") else "")
                               + "\nIt uses the same checks as an engineer's Execute. An engineer can press *Stop* "
                                 "on its message at any time."))
    elif proposal.get("executable"):
        par = proposal.get("parameter") or {}
        failed = [c["detail"] for c in (gate or {}).get("checks", []) if not c["ok"]]
        if failed:
            blocks.append(_context(":raised_hand: Not automated: " + "; ".join(failed[:3])))
        blocks.append(_section(":raised_hand: *Human approval required.* Proposed action: "
                               f"{proposal['summary']}"
                               + (f" — the agent suggests {par['name']} = `{par['value']}`" if par.get("value")
                                  else "")
                               + "\nUse the plan's buttons below: *Approve*"
                               + (" (type the value you choose)" if par else "")
                               + ", then *Execute*. Nothing changes until a human approves and executes."))
    else:
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(proposal.get("manual_steps") or [], 1))
        blocks.append(_section(f":no_entry: *No automatic remediation:* {proposal.get('reason')}."
                               + (f"\n*Recommended manual steps (from the agent):*\n{steps}" if steps else "")
                               + "\nThe platform only executes typed, policy-checked actions; this one needs an engineer."))
    return {"text": f"Investigator Agent report for {inc['id']}", "blocks": blocks[:50]}


def resolved(inc: dict, record: dict) -> dict:
    v = (record or {}).get("verification") or {}
    checks = "\n".join(f":white_check_mark: {c['statement']}" for c in (v.get("criteria") or [])[:6])
    return {"text": f"{inc['id']} resolved", "blocks": [_section(
        f":large_green_circle: *INCIDENT RESOLVED* — {inc['id']}\nThe approved change was applied and the deterministic "
        f"validation passed:\n{checks}")]}


def not_resolved(inc: dict, record: dict) -> dict:
    v = (record or {}).get("verification") or {}
    return {"text": f"{inc['id']} not resolved", "blocks": [_section(
        f":red_circle: *{(record or {}).get('outcome')}* — {inc['id']}\n{v.get('reason', '')[:500]}\n"
        f"No further change was made.")]}


# --------------------------------------------------------------------------- the Remediation Agent

OUTCOME_ICON = {"RESOLVED": ":large_green_circle:", "NOT_RESOLVED": ":red_circle:", "INCONCLUSIVE": ":white_circle:"}


def _btn(text: str, action_id: str, value: str, style: str | None = None) -> dict:
    b = {"type": "button", "text": {"type": "plain_text", "text": text}, "action_id": action_id, "value": value}
    return {**b, "style": style} if style else b


def auto_start(inc: dict, ladder: list, proposal: dict, gate: dict) -> dict:
    checks = "\n".join(f":white_check_mark: {c['detail']}" for c in gate.get("checks", []))
    steps = " → ".join(f"`{v}`" for v in ladder)
    text = (f":robot_face: *Remediation Agent — auto-approved* {inc['id']}\n"
            f"*Action:* {proposal['summary']}\n*Attempts (policy ladder):* {steps}"
            + (" — the next one only if the previous was not verified as resolved" if len(ladder) > 1 else "")
            + "\nIf no attempt is verified as resolved, every change is reverted and an engineer must take over.")
    return {"text": f"Remediation Agent auto-approved {inc['id']}", "blocks": [
        _section(text), _section("*Why it may act alone (every check passed):*\n" + checks),
        {"type": "actions", "elements": [_btn("Stop automatic remediation", "auto:stop", inc["id"], "danger")]},
        _context("Stop: no further attempt and no revert; you take over the incident.")]}


def auto_attempt(inc: dict, att: dict) -> dict:
    rec = att.get("record") or {}
    lines = [f":gear: *Remediation Agent — attempt {att['n']} of {att['of']}*: value `{att['value']}`"]
    stage = att.get("stage")
    if stage == "approved":
        lines.append("Approval recorded by the Remediation Agent; executing with the standard checks …")
    elif stage in ("checks_passed", "applied"):
        lines.append("Policy ✓ · claim ✓ · live recheck, dry run and the change follow …" if stage == "checks_passed"
                     else f"Live recheck ✓ · dry run ✓ · applied `{(rec.get('applied') or {}).get('before')}` → "
                          f"`{(rec.get('applied') or {}).get('after')}` · verifying with the plan's criteria …")
    elif stage == "done":
        if att.get("after"):
            lines.append(f"Applied `{att.get('before')}` → `{att.get('after')}`")
        v = att.get("verification") or {}
        if att.get("outcome"):
            crit = "\n".join(f"{'✓' if c['status'] == 'pass' else '✗' if c['status'] == 'fail' else '?'} "
                             f"{c['statement']} — _{(c.get('evidence') or '')[:140]}_" for c in v.get("criteria", [])[:6])
            lines.append(f"{OUTCOME_ICON.get(att['outcome'], '')} *{att['outcome']}* — {(v.get('reason') or '')[:300]}"
                         + (f"\n{crit}" if crit else ""))
        else:
            lines.append(f":no_entry: Stopped: {(att.get('message') or '')[:400]}")
    elif stage == "stopped":
        lines.append(f":no_entry: Stopped: {(rec.get('message') or '')[:400]}")
    return {"text": f"Remediation Agent attempt {att['n']}: {att.get('outcome') or stage}",
            "blocks": [_section("\n".join(lines))]}


def auto_revert(inc: dict, info: dict) -> dict:
    if info.get("stage") == "started":
        text = (f":rewind: *Remediation Agent — reverting* `{info.get('from')}` → `{info.get('to')}` (the value before "
                f"the first attempt; policy: always revert a remediation that did not work) …")
    elif info.get("ok"):
        text = (f":rewind: *Reverted* `{info.get('before')}` → `{info.get('after')}`. The system is back in the state "
                f"the investigation described.")
    else:
        text = f":warning: *Revert not completed:* {(info.get('message') or '')[:400]}"
    return {"text": "Remediation Agent revert", "blocks": [_section(text)]}


def auto_handover(inc: dict, out: dict) -> dict:
    tried = "\n".join(f"{a['n']}. `{a['value']}` → *{a.get('outcome') or a.get('code')}*" for a in out["attempts"])
    rv = out.get("revert")
    rv_text = ("" if not rv else f"\n*Revert:* `{rv.get('before')}` → `{rv.get('after')}`" if rv.get("ok")
               else f"\n*Revert:* not completed — {(rv.get('message') or '')[:200]}")
    blocks = [_section(f":red_circle: *Automatic remediation did not work — an engineer must fix this manually.*\n"
                       f"{inc['id']} · {out.get('reason')}\n*Attempts:*\n{tried or '_none_'}{rv_text}")]
    if out.get("stopped_by"):
        blocks.append(_context(f"Stopped by <@{out['stopped_by']}>, who owns the incident now."))
    else:
        blocks += [_section(f"`{out.get('component')}` is now *locked* against automatic remediation until an "
                            f"engineer acknowledges."),
                   {"type": "actions", "elements": [_btn("Acknowledge — I will fix it manually", "auto:ack", inc["id"],
                                                         "primary")]}]
    return {"text": f"{inc['id']}: automatic remediation failed; engineer action required", "blocks": blocks}


def auto_resolved(inc: dict, out: dict) -> dict:
    a = out["resolved_by"]
    crit = "\n".join(f":white_check_mark: {c['statement']}" for c in (a.get("verification") or {}).get("criteria", [])[:6])
    return {"text": f"{inc['id']} resolved automatically", "blocks": [_section(
        f":large_green_circle: *INCIDENT RESOLVED automatically* — {inc['id']}\nThe Remediation Agent applied "
        f"`{a.get('before')}` → `{a.get('after')}` (attempt {a['n']} of {a['of']}) and the deterministic validation "
        f"passed:\n{crit}")]}


def auto_stopped(inc: dict, user: str) -> dict:
    return {"text": "Automatic remediation stopped", "blocks": [_section(
        f":octagonal_sign: <@{user}> stopped automatic remediation of {inc['id']}. The current step finishes; no "
        f"further attempt and no revert will be made. <@{user}> owns the incident.")]}


def auto_acknowledged(inc: dict, user: str, components: list) -> dict:
    return {"text": "Handover acknowledged", "blocks": [_section(
        f":white_check_mark: <@{user}> acknowledged {inc['id']} and is fixing it manually."
        + (f" Automatic remediation is re-enabled for {', '.join('`' + c + '`' for c in components)}." if components
           else ""))]}
