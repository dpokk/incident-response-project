"""Demo: the Investigator Agent's guardrails (read-only tools, argument validation, secret masking, citation checks)
and its loop, against the fake cluster and a scripted model. No network, no cluster."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fake_cluster as fc  # noqa: E402
from test_history import investigate  # noqa: E402

from demo.agent.investigator import InvestigatorAgent, validate_report  # noqa: E402
from demo.agent.llm import LLMError  # noqa: E402
from demo.agent.tools import ToolBox, ToolRejected  # noqa: E402
from investigator.capabilities import Capabilities  # noqa: E402
from investigator.capabilities.kubernetes import KubernetesAdapter  # noqa: E402
from investigator.config import Settings  # noqa: E402
from investigator.evidence import EvidenceStore  # noqa: E402

NOW = fc.NOW


def toolbox(world):
    dx, facts, report = investigate(world)
    caps = Capabilities(KubernetesAdapter(fc.FakeKube(world), "shop"), EvidenceStore())
    return ToolBox(caps, Settings(), report, NOW - 600, clock=lambda: NOW), report


def test_tools_are_read_only_and_validate_arguments():
    tb, _ = toolbox(fc.world_db_down())
    names = {s["function"]["name"] for s in tb.specs()}
    assert not any(w in n for n in names for w in ("set_", "patch", "scale", "exec", "delete", "apply", "kubectl"))
    with pytest.raises(ToolRejected):
        tb.call("scale_workload", {"component": "postgres"})          # no such tool exists
    with pytest.raises(ToolRejected):
        tb.call("get_resource_state", {"component": "kube-system"})   # unknown component
    with pytest.raises(ToolRejected):
        tb.call("get_service_health", {"host": "postgres; rm -rf /"})
    res, rec = tb.call("get_resource_state", {"component": "postgres", "purpose": "check replicas"})
    assert rec["id"] == "E1" and res["desired"] == 0 and "0/0 ready" in rec["summary"]


def test_secrets_are_masked_and_active_probes_are_budgeted():
    tb, _ = toolbox(fc.world_db_misconfig())
    res, _ = tb.call("get_configuration", {"component": "postgres"})
    assert all(e["value"] == "***" for e in res["entries"] if e["source"].startswith("secret/"))
    assert "s3cret" not in json.dumps(res)
    for _ in range(3):
        tb.call("probe_request", {})
    with pytest.raises(ToolRejected):
        tb.call("probe_request", {})


def test_report_citations_and_components_are_checked():
    tb, report = toolbox(fc.world_db_down())
    tb.call("get_resource_state", {"component": "postgres"})
    fid = report["evidence"][0]["id"]
    r = validate_report({"summary": "s", "category": "dependency_unavailable", "root_cause_component": "postgres",
                         "root_cause": "postgres scaled to 0", "confidence": "high",
                         "findings": [{"statement": "ok", "evidence_ids": ["E1", fid]},
                                      {"statement": "made up", "evidence_ids": ["E99"]}],
                         "suggested_fix": {"action": "scale_workload", "target": "postgres", "parameter_value": "1",
                                           "description": "scale up", "evidence_ids": ["E1"]}}, tb)
    assert [f["supported"] for f in r["findings"]] == [True, False]
    assert r["findings"][1]["invalid_citations"] == ["E99"] and not r["validation"]["ok"]
    bad = validate_report({"root_cause_component": "nonexistent", "suggested_fix": {"action": "run_shell"},
                           "findings": []}, tb)
    assert bad["suggested_fix"]["action"] == "investigate_only" and not bad["root_cause_component_valid"]


class ScriptedModel:
    """A fake model that calls two tools, then submits a report."""
    name = "scripted"

    def __init__(self, fail=False):
        self.turn, self.fail = 0, fail

    def chat(self, messages, tools, tool_choice="auto", max_tokens=1500):
        if self.fail:
            raise LLMError("model unavailable")
        self.turn += 1
        call = lambda name, args: {"id": f"c{self.turn}", "type": "function",  # noqa: E731
                                   "function": {"name": name, "arguments": json.dumps(args)}}
        if self.turn == 1:
            tc = [call("get_rule_findings", {"purpose": "see what the rules found"})]
        elif self.turn == 2:
            tc = [call("get_resource_state", {"component": "postgres", "purpose": "replicas"})]
        else:
            tc = [call("submit_report", {"summary": "postgres is at 0", "category": "dependency_unavailable",
                                         "root_cause_component": "postgres", "root_cause": "scaled to 0",
                                         "confidence": "high", "findings": [{"statement": "0 replicas",
                                                                             "evidence_ids": ["E2"]}],
                                         "suggested_fix": {"action": "scale_workload", "target": "postgres",
                                                           "parameter_value": "1", "description": "scale to 1"}})]
        return {"message": {"content": "", "tool_calls": tc}, "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "latency_s": 0.0}


def test_agent_loop_streams_real_steps_and_returns_a_validated_report():
    tb, _ = toolbox(fc.world_db_down())
    events = []
    out = InvestigatorAgent(ScriptedModel(), tb, lambda t, d: events.append(t)).run({"id": "INC-T", "signals": []})
    assert out["status"] == "ok" and out["tool_calls"] == 2
    assert out["report"]["findings"][0]["supported"] and out["report"]["validation"]["ok"]
    assert events.count("agent_tool_call") == 2 and events.count("agent_tool_result") == 2


def test_agent_unavailable_is_reported_not_hidden():
    tb, _ = toolbox(fc.world_db_down())
    events = []
    out = InvestigatorAgent(ScriptedModel(fail=True), tb, lambda t, d: events.append(t)).run({"id": "INC-T"})
    assert out["status"] == "unavailable" and "agent_error" in events
