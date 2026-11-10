"""Demo: the Reviewer Agent. It checks the Investigator Agent's report with the same read-only tools, its objections
count only with valid evidence, it falls back to a second model, and it can only block automation, never enable it.
Scripted models and the fake cluster; no network, no cluster."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fake_cluster as fc  # noqa: E402
from test_demo_agent import ScriptedModel, toolbox  # noqa: E402
from test_demo_mirror import engine  # noqa: E402
from test_demo_remediation_agent import POLICY, good_inputs  # noqa: E402

from demo import slack_ai  # noqa: E402
from demo.agent.investigator import InvestigatorAgent  # noqa: E402
from demo.agent.llm import DEFAULT_REVIEWER_MODEL, ChatModel, LLMError  # noqa: E402
from demo.agent.reviewer import review, validate_review  # noqa: E402
from demo.remediation_agent import AutoPolicy, gate  # noqa: E402


def investigated(world=None):
    """The Investigator's ToolBox (with its evidence E1, E2) and its validated report."""
    tb, _ = toolbox(world or fc.world_db_down())
    out = InvestigatorAgent(ScriptedModel(), tb, lambda t, d: None).run({"id": "INC-T", "signals": []})
    return tb, out["report"]


class Reviewer:
    """A scripted reviewer: one read-only tool call, then submit_review with the given arguments."""

    def __init__(self, verdict="agree", objections=(), fail=False, name="scripted-reviewer"):
        self.verdict, self.objections, self.fail, self.model, self.turn = verdict, list(objections), fail, name, 0
        self.tools_seen = []

    @property
    def name(self):
        return self.model

    def chat(self, messages, tools, tool_choice="auto", max_tokens=1500):
        if self.fail:
            raise LLMError("model returned HTTP 404")
        self.turn += 1
        self.tools_seen = [t["function"]["name"] for t in tools]
        if self.turn == 1:
            name, args = "get_resource_state", {"component": "postgres", "purpose": "re-check replicas"}
        else:
            name, args = "submit_review", {"verdict": self.verdict, "summary": "checked",
                                           "objections": self.objections}
        return {"message": {"content": "", "tool_calls": [{"id": f"r{self.turn}", "type": "function", "function": {
            "name": name, "arguments": json.dumps(args)}}]}, "usage": {}, "latency_s": 0.0}


def run(models, tb=None, report=None):
    if tb is None:
        tb, report = investigated()
    events = []
    out = review(models, tb, lambda t, d: events.append((t, d)), report, "rules", {"id": "INC-T", "signals": []})
    return out, events, tb


# --------------------------------------------------------------------------- the agent

def test_the_reviewer_uses_the_investigators_evidence_and_only_read_tools():
    m = Reviewer()
    out, events, tb = run([m])
    assert out["status"] == "ok" and out["review"]["effective"] == "agree" and out["tool_calls"] == 1
    assert "submit_review" in m.tools_seen and "submit_report" not in m.tools_seen
    assert not any(w in n for n in m.tools_seen for w in ("set_", "patch", "scale", "exec", "delete", "apply"))
    assert "E3" in tb.evidence                     # its own call continues the Investigator's evidence (E1, E2)
    assert [t for t, _ in events].count("agent_tool_call") == 1


def test_an_objection_counts_only_with_valid_evidence():
    tb, _ = investigated()
    ok = validate_review({"verdict": "challenge", "summary": "s",
                          "objections": [{"statement": "postgres has replicas", "evidence_ids": ["E1"]}]}, tb)
    assert ok["effective"] == "challenge" and ok["objections"][0]["supported"]
    made_up = validate_review({"verdict": "reject", "objections": [{"statement": "x", "evidence_ids": ["E99"]}]}, tb)
    assert made_up["effective"] == "unsupported" and made_up["objections"][0]["invalid_citations"] == ["E99"]
    assert validate_review({"verdict": "reject", "objections": []}, tb)["effective"] == "unsupported"
    assert validate_review({"verdict": "approve"}, tb)["effective"] == "unsupported"


def test_an_unavailable_model_falls_back_to_the_next_then_gives_up():
    out, events, _ = run([Reviewer(fail=True, name="m-primary"), Reviewer(name="m-fallback")])
    assert out["status"] == "ok" and out["model"] == "m-fallback"
    assert any(t == "agent_note" and "m-primary unavailable" in d["text"] for t, d in events)
    out, _, _ = run([Reviewer(fail=True), Reviewer(fail=True)])
    assert out["status"] == "unavailable" and "review" not in out


def test_reviewer_models_from_the_environment(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    monkeypatch.setenv("DEMO_AGENT_MODEL", "big-model")
    monkeypatch.delenv("DEMO_REVIEWER_MODEL", raising=False)
    assert [m.model for m in ChatModel.reviewers_from_env()] == [DEFAULT_REVIEWER_MODEL, "big-model"]
    monkeypatch.setenv("DEMO_REVIEWER_MODEL", "")
    assert [m.model for m in ChatModel.reviewers_from_env()] == ["big-model"]
    monkeypatch.delenv("NVIDIA_API_KEY")
    assert ChatModel.reviewers_from_env() == []


# --------------------------------------------------------------------------- it can only block

def reviewed(effective):
    return {"status": "ok", "review": {"effective": effective}}


def test_the_review_can_only_block_the_gate():
    pol = AutoPolicy.load(POLICY)
    route, ai, p = good_inputs()
    for rv in (None, {"status": "unavailable", "error": "timeout"}, {"status": "skipped"}, reviewed("agree"),
               reviewed("unsupported")):
        assert gate(pol, True, route, ai, p, {}, review=rv)["eligible"], rv
    for eff in ("challenge", "reject"):
        g = gate(pol, True, route, ai, p, {}, review=reviewed(eff))
        assert not g["eligible"] and f"Reviewer Agent {eff}s" in [c for c in g["checks"] if not c["ok"]][0]["detail"]
    assert not gate(pol, False, route, ai, p, {}, review=reviewed("agree"))["eligible"]   # agreeing enables nothing


# --------------------------------------------------------------------------- the engine and Slack

def test_the_engine_reviews_on_the_investigators_toolbox_and_renames_its_events(tmp_path, monkeypatch):
    eng, bus, _ = engine(tmp_path, monkeypatch)
    tb, report = investigated()
    eng._agent_tb, eng.reviewers = tb, [Reviewer("challenge", [{"statement": "postgres is fine", "evidence_ids": ["E1"]}])]
    inc = {"id": "INC-T", "signals": []}
    out = eng._review(inc, {"failure_category_label": "x", "confidence": 0.9}, {"status": "ok", "report": report})
    types = [e["type"] for e in bus._history]
    assert out["review"]["effective"] == "challenge" and inc["review"] == "challenge"
    assert "reviewer_tool_call" in types and "review_result" in types and "agent_tool_call" not in types
    skipped = eng._review(inc, {}, {"status": "unavailable"})
    assert skipped["status"] == "skipped"                      # nothing to review: no Investigator report


def test_the_slack_reply_shows_the_verdict_and_its_evidence():
    inc = {"id": "INC-T"}
    tb, _ = investigated()
    rv = {"status": "ok", "tool_calls": 2, "seconds": 5.0, "model": "m", "review": validate_review(
        {"verdict": "reject", "summary": "wrong component",
         "objections": [{"statement": "postgres is ready", "evidence_ids": ["E1"]}]}, tb)}
    text = json.dumps(slack_ai.review(inc, rv))
    assert "rejects the conclusion" in text and "postgres is ready" in text and "E1" in text
    assert "unavailable" in json.dumps(slack_ai.review(inc, {"status": "unavailable", "error": "timeout"}))
