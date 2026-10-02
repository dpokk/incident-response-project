"""Demo: hybrid routing between the rule engine and the Investigator Agent, and who decides the executable action."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fake_cluster as fc  # noqa: E402
from test_history import investigate  # noqa: E402

from demo.engine import Engine, route  # noqa: E402


def rule_report(world):
    dx, facts, report = investigate(world)
    from investigator.remediation import plan_remediation
    report["remediation_plan"] = plan_remediation(dx, report["reconstruction"], report["impact"], facts,
                                                  "INC-T").to_dict()
    return report


def test_known_patterns_are_led_by_the_rules_and_others_by_the_agent():
    assert route({"failure_category": "dependency_unavailable", "confidence": 0.97,
                  "failure_category_label": "Dependency unavailable"})["mode"] == "verify"
    r = route({"failure_category": "dependency_misconfiguration", "confidence": 0.6, "failure_category_label": "x"})
    assert r["mode"] == "lead" and "60%" in r["reason"] and r["budget"]["tool_calls"] > 5
    assert route({"failure_category": "undetermined", "confidence": 0.9})["mode"] == "lead"
    assert route({})["mode"] == "lead"


def ai(action, value=None, manual=None):
    return {"status": "ok", "report": {"suggested_fix": {"action": action, "target": "x", "parameter_value": value,
                                                         "description": "d"}, "manual_steps": manual or []}}


def test_rules_lead_the_executable_action_comes_from_the_plan_with_the_agents_value():
    rep = rule_report(fc.world_oom())
    p = Engine._proposal(None, rep, ai("adjust_resource_limit", "384Mi"), {"mode": "verify"})
    assert p["executable"] and p["action_type"] == "adjust_resource_limit" and p["parameter"]["value"] == "384Mi"
    p = Engine._proposal(None, rep, ai("investigate_only"), {"mode": "verify"})   # the agent declines: rules still lead
    assert p["executable"] and p["source"] == "rules" and p["parameter"]["value"] is None
    p = Engine._proposal(None, rep, ai("adjust_resource_limit", "64Ti"), {"mode": "verify"})
    assert p["parameter"]["value"] is None and "rejected" in p["parameter"]["error"]      # bounded deterministically


def test_agent_leads_its_manual_fix_is_shown_and_nothing_becomes_executable():
    rep = rule_report(fc.world_crash())                                          # the plan has no typed action
    p = Engine._proposal(None, rep, ai("investigate_only", manual=["point PGPASSWORD back"]), {"mode": "lead"})
    assert not p["executable"] and p["manual_steps"] == ["point PGPASSWORD back"] and p["led_by"] == "agent"
    p = Engine._proposal(None, rep, ai("scale_workload", "3"), {"mode": "lead"})  # not eligible in the plan
    assert not p["executable"] and "no eligible" in p["reason"]
