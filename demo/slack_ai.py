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


def report(inc: dict, ai: dict, proposal: dict, rule: dict) -> dict:
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"Investigator Agent report — {inc['id']}"}}]
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
    if proposal.get("executable"):
        par = proposal.get("parameter") or {}
        blocks.append(_section(":raised_hand: *Human approval required.* Proposed action: "
                               f"{proposal['summary']}"
                               + (f" — {par['name']} = `{par['value']}` (suggested by the agent)" if par.get("value")
                                  else "")
                               + "\nApprove in the incident console, or with the plan's buttons below. "
                                 "Nothing changes until a human approves."))
    else:
        blocks.append(_section(f":no_entry: No executable remediation: {proposal.get('reason')}."))
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
