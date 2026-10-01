"""Iteration 6: the Slack incident thread (presentation) and human review (durable decisions). Nothing executes."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fake_cluster as fc  # noqa: E402
from test_history import fill_oom_replaced_history, history, investigate, world_oom_pods_replaced  # noqa: E402

from investigator import slack, slack_app, slack_view  # noqa: E402
from investigator.remediation import plan_remediation  # noqa: E402
from investigator.review import ReviewService, plan_digest  # noqa: E402

NOW = fc.NOW
APPROVER, OTHER = "U0APPROVER", "U0INTRUDER"


def make_plan(world=None, store=None):
    dx, facts, report = investigate(world or fc.world_oom(), store)
    plan = plan_remediation(dx, report["reconstruction"], report["impact"], facts, "INC-TEST").to_dict()
    report["remediation_plan"] = plan
    return plan, report


def service(tmp_path, plan=None, approvers=(APPROVER,)):
    svc = ReviewService(tmp_path / "reviews.db", approvers, clock=lambda: NOW)
    digest = svc.register_plan("INC-TEST", plan, NOW - 60) if plan else None
    return svc, digest


def text_of(payload) -> str:
    out = []
    for b in payload["blocks"]:
        if "text" in b and isinstance(b["text"], dict):
            out.append(b["text"]["text"])
        out += [f["text"] for f in b.get("fields", [])]
        out += [e["text"] if isinstance(e.get("text"), str) else e.get("text", {}).get("text", "")
                for e in b.get("elements", [])]
    return "\n".join(out)


def buttons(payload) -> list[str]:
    return [e["action_id"] for b in payload["blocks"] if b["type"] == "actions" for e in b["elements"]]


# --------------------------------------------------------------------------- rendering (presentation only)

def test_actionable_plan_renders_every_review_element():
    plan, _ = make_plan()
    msg = slack_view.plan_message(plan, plan_digest(plan), {}, NOW - 60, NOW)
    t = text_of(msg)
    assert "Remediation plan — proposed, not executed" in t and "Action proposed" in t
    for part in ("*Why:*", "*Risks:*", "*Rollback:*", "*Verification", "*Expected final state:*",
                 "Needs your input", "*Confidence:*", "Requires human approval", "nothing is executed"):
        assert part in t, part
    assert buttons(msg) == ["review:approved:0", "review:rejected:0", "review:investigate_first:0",
                            "review:acknowledged:1"]
    assert [b["block_id"] for b in msg["blocks"] if b["type"] == "input"] == ["param:0"]
    json.dumps(msg)


def test_each_assessment_renders_without_change_controls_where_none_apply(tmp_path):
    store, clock = history(tmp_path)
    fill_oom_replaced_history(store, clock)
    for world, st, expected in ((fc.world_crash(), None, "No safe action available"),
                                (fc.world_healthy_after_rollout(), None, "Investigate further"),
                                (world_oom_pods_replaced(), store, "No immediate action needed")):
        plan, _ = make_plan(world, st)
        msg = slack_view.plan_message(plan, plan_digest(plan), {}, NOW - 60, NOW)
        assert expected in text_of(msg)
        change_buttons = [b for b in buttons(msg) if not b.startswith("review:acknowledged")]
        changes = [a for a in plan["actions"] if a["type"] != "investigate_further"]
        assert bool(change_buttons) == bool(changes), (expected, buttons(msg))


def test_plan_age_is_shown_and_an_old_plan_is_flagged():
    plan, _ = make_plan()
    fresh = text_of(slack_view.plan_message(plan, plan_digest(plan), {}, NOW - 120, NOW))
    old = text_of(slack_view.plan_message(plan, plan_digest(plan), {}, NOW - 3 * 3600, NOW))
    assert "(2 min ago)" in fresh and ":warning:" not in fresh
    assert "(3.0 h ago)" in old and "reflects the state observed at" in old


def test_uncertainty_and_competing_causes_are_shown():
    plan, _ = make_plan(fc.world_crash_caused_by_db_down())
    t = text_of(slack_view.plan_message(plan, plan_digest(plan), {}, NOW, NOW))
    assert "Competing explanations:" in t and "application_crash in backend" in t


def test_recorded_decisions_replace_the_controls():
    plan, _ = make_plan()
    statuses = {0: {"status": "approved", "reviewer": APPROVER, "at": NOW, "supplied_parameters": {"proposed_limit": "256Mi"}},
                1: {"status": "acknowledged", "reviewer": APPROVER, "at": NOW}}
    msg = slack_view.plan_message(plan, plan_digest(plan), statuses, NOW, NOW)
    assert buttons(msg) == [] and "proposed_limit = `256Mi`" in text_of(msg) and f"<@{APPROVER}>" in text_of(msg)
    sup = slack_view.plan_message(plan, plan_digest(plan), {0: {"status": "superseded"}, 1: {"status": "superseded"}}, NOW, NOW)
    assert buttons(sup) == [] and "superseded by a newer plan" in text_of(sup)


def test_investigation_message_shows_the_diagnosis_from_the_report():
    _, report = make_plan()
    t = text_of(slack_view.investigation_message(report))
    for part in (report["failure_category_label"], "Affected component", "Root-cause component", "Likely root cause",
                 "Evidence", "Confidence", "Impact"):
        assert part in t, part


# --------------------------------------------------------------------------- review model (independent of Slack)

def test_authorized_approval_with_a_supplied_value_is_recorded_exactly(tmp_path):
    plan, _ = make_plan()
    svc, d = service(tmp_path, plan)
    out = svc.decide("INC-TEST", d, 0, "approved", APPROVER, supplied="256Mi", comment="checked capacity")
    assert out.effective and out.status == "approved"
    [rec] = svc.decisions("INC-TEST", effective_only=True)
    assert (rec["plan_digest"], rec["action_index"], rec["action_type"], rec["reviewer"]) == \
        (d, 0, "adjust_resource_limit", APPROVER)
    assert rec["supplied_parameters"] == {"proposed_limit": "256Mi"} and rec["comment"] == "checked capacity"
    assert svc.statuses("INC-TEST", d)[0]["status"] == "approved"
    reopened = ReviewService(tmp_path / "reviews.db", [APPROVER])                    # durable across restarts
    assert reopened.statuses("INC-TEST", d)[0]["supplied_parameters"] == {"proposed_limit": "256Mi"}


def test_unauthorized_users_cannot_decide(tmp_path):
    plan, _ = make_plan()
    svc, d = service(tmp_path, plan)
    out = svc.decide("INC-TEST", d, 0, "approved", OTHER, supplied="256Mi")
    assert not out.effective and "not an authorised approver" in out.message
    assert svc.statuses("INC-TEST", d)[0]["status"] == "awaiting_review"
    [attempt] = svc.decisions("INC-TEST")
    assert attempt["effective"] is False and attempt["reason"] == "unauthorized"       # audited, not effective
    nobody, d2 = service(tmp_path / "x", plan, approvers=())
    assert not nobody.decide("INC-TEST", d2, 0, "rejected", APPROVER).effective        # empty allowlist: nobody


def test_rejection_and_per_action_decisions(tmp_path):
    plan, _ = make_plan()
    svc, d = service(tmp_path, plan)
    assert svc.decide("INC-TEST", d, 0, "rejected", APPROVER).effective
    assert svc.decide("INC-TEST", d, 1, "acknowledged", APPROVER).effective
    st = svc.statuses("INC-TEST", d)
    assert (st[0]["status"], st[1]["status"]) == ("rejected", "acknowledged")


def test_an_investigation_step_can_only_be_acknowledged(tmp_path):
    plan, _ = make_plan()
    svc, d = service(tmp_path, plan)
    out = svc.decide("INC-TEST", d, 1, "approved", APPROVER)
    assert not out.effective and out.record == {} and "not a valid decision" in out.message


def test_decisions_are_bound_to_the_exact_plan_digest(tmp_path):
    plan, _ = make_plan()
    svc, d = service(tmp_path, plan)
    assert not svc.decide("INC-TEST", "0" * 64, 0, "rejected", APPROVER).effective
    altered = json.loads(json.dumps(plan))
    altered["actions"][0]["summary"] += " (edited)"
    assert plan_digest(altered) != d and plan_digest(json.loads(json.dumps(plan))) == d   # any change changes it


def test_a_newer_plan_supersedes_the_old_plan_and_its_decisions(tmp_path):
    plan, _ = make_plan()
    svc, old = service(tmp_path, plan)
    assert svc.decide("INC-TEST", old, 0, "approved", APPROVER, supplied="256Mi").effective
    newer = json.loads(json.dumps(plan))
    newer["current_state"].append({"statement": "re-investigated: new evidence", "fact_ids": []})
    new = svc.register_plan("INC-TEST", newer, NOW)
    assert new != old and svc.current_digest("INC-TEST") == new
    assert all(s["status"] == "superseded" for s in svc.statuses("INC-TEST", old).values())
    assert svc.statuses("INC-TEST", new)[0]["status"] == "awaiting_review"            # the approval did not carry over
    late = svc.decide("INC-TEST", old, 0, "rejected", APPROVER)
    assert not late.effective and late.status == "superseded"
    assert svc.register_plan("INC-TEST", newer, NOW) == new                            # re-registering is a no-op


def test_duplicate_and_conflicting_clicks(tmp_path):
    plan, _ = make_plan()
    svc, d = service(tmp_path, plan)
    assert svc.decide("INC-TEST", d, 0, "rejected", APPROVER).effective
    dup = svc.decide("INC-TEST", d, 0, "rejected", APPROVER)
    conflict = svc.decide("INC-TEST", d, 0, "approved", APPROVER, supplied="256Mi")
    assert not dup.effective and "nothing changed" in dup.message
    assert not conflict.effective and "not recorded" in conflict.message
    assert [r["decision"] for r in svc.decisions("INC-TEST", effective_only=True)] == ["rejected"]


def test_incomplete_parameters_must_be_supplied_and_valid(tmp_path):
    plan, _ = make_plan()                                        # adjust_resource_limit, current 192Mi, no value
    svc, d = service(tmp_path, plan)
    assert svc.decide("INC-TEST", d, 0, "approved", APPROVER).message.startswith("Approval needs")
    for bad in ("lots", "128Mi", "192Mi", "999Gi", "-5Mi"):
        out = svc.decide("INC-TEST", d, 0, "approved", APPROVER, supplied=bad)
        assert not out.effective and out.message.startswith("Not recorded"), bad
    assert svc.statuses("INC-TEST", d)[0]["status"] == "awaiting_review"
    assert svc.decide("INC-TEST", d, 0, "approved", APPROVER, supplied=" 1Gi ").record["supplied_parameters"] == \
        {"proposed_limit": "1Gi"}


def test_a_candidate_value_is_never_used_implicitly(tmp_path):
    plan, _ = make_plan(fc.world_db_misconfig())                # candidate_host 'postgres' is only a suggestion
    svc, d = service(tmp_path, plan)
    assert plan["actions"][0]["parameters"]["candidate_host"] == "postgres"
    assert not svc.decide("INC-TEST", d, 0, "approved", APPROVER).effective
    assert not svc.decide("INC-TEST", d, 0, "approved", APPROVER, supplied="postgres-wrong").effective  # current
    out = svc.decide("INC-TEST", d, 0, "approved", APPROVER, supplied="postgres")
    assert out.effective and out.record["supplied_parameters"] == {"restore_to_host": "postgres"}


def test_replica_parameters_are_validated(tmp_path):
    plan, _ = make_plan(fc.world_db_down())
    svc, d = service(tmp_path, plan)
    for bad in ("one", "0", "51", "1.5"):
        assert not svc.decide("INC-TEST", d, 0, "approved", APPROVER, supplied=bad).effective, bad
    assert svc.decide("INC-TEST", d, 0, "approved", APPROVER, supplied="1").record["supplied_parameters"] == \
        {"target_replicas": 1}


# --------------------------------------------------------------------------- Slack interaction + thread (fake transport)

class FakeTransport(slack.Transport):
    def __init__(self, threaded=True):
        self.posts, self.updates, self.responses, self._threaded, self.n = [], [], [], threaded, 0

    @property
    def threaded(self):
        return self._threaded

    @property
    def configured(self):
        return True

    def post(self, payload, thread_ts=None):
        self.n += 1
        self.posts.append((thread_ts, payload))
        return ("C1", f"ts{self.n}") if self._threaded else (None, None)

    def update(self, channel, ts, payload):
        self.updates.append((ts, payload))
        return self._threaded

    def respond(self, url, payload):
        self.responses.append(payload)
        return True


def click(decision, index, digest, user=APPROVER, value=None):
    payload = {"type": "block_actions", "user": {"id": user}, "response_url": "https://hooks.slack.test/r",
               "actions": [{"action_id": f"review:{decision}:{index}",
                            "value": json.dumps({"incident": "INC-TEST", "digest": digest, "action": index})}],
               "state": {"values": {}}}
    if value is not None:
        payload["state"]["values"][f"param:{index}"] = {"value": {"type": "plain_text_input", "value": value}}
    return payload


def test_the_incident_is_one_thread_and_the_review_updates_it(tmp_path):
    plan, report = make_plan()
    report["id"] = "INC-TEST"
    svc, d = service(tmp_path, plan)
    tr, reg = FakeTransport(), slack.ThreadRegistry(tmp_path / "threads.json")
    slack.publish_detection(tr, reg, {"id": "INC-TEST", "signals": [{"text": "restarts"}]}, "shop")
    assert slack.publish_investigation(tr, reg, report, svc, d, "shop", NOW)
    root = reg.get("INC-TEST")["root_ts"]
    assert [t for t, _ in tr.posts] == [None, root, root]                  # detection root, then two replies
    assert reg.get("INC-TEST")["plan_ts"] == "ts3" and reg.get("INC-TEST")["digest"] == d

    out = slack_app.handle_interaction(click("approved", 0, d, value="256Mi"), svc, tr, reg, NOW, log=lambda *_: None)
    assert out["effective"] and out["record"]["supplied_parameters"] == {"proposed_limit": "256Mi"}
    assert any(ts == "ts3" for ts, _ in tr.updates)                         # plan message re-rendered in place
    assert "approved" in text_of(tr.posts[-1][1]) and tr.posts[-1][0] == root   # decision announced in the thread
    assert any(ts == root and "Remediation review" in text_of(p) for ts, p in tr.updates)  # root shows review status


def test_a_refused_click_only_tells_the_clicker(tmp_path):
    plan, _ = make_plan()
    svc, d = service(tmp_path, plan)
    tr, reg = FakeTransport(), slack.ThreadRegistry(tmp_path / "threads.json")
    out = slack_app.handle_interaction(click("approved", 0, d, user=OTHER, value="256Mi"), svc, tr, reg, NOW,
                                       log=lambda *_: None)
    assert not out["effective"] and tr.posts == [] and tr.updates == []
    assert tr.responses[-1]["response_type"] == "ephemeral" and "not an authorised approver" in tr.responses[-1]["text"]


def test_webhook_only_mode_still_reviews_through_the_response_url(tmp_path):
    plan, _ = make_plan()
    svc, d = service(tmp_path, plan)
    tr, reg = FakeTransport(threaded=False), slack.ThreadRegistry(tmp_path / "threads.json")
    out = slack_app.handle_interaction(click("rejected", 0, d), svc, tr, reg, NOW, log=lambda *_: None)
    assert out["effective"]
    assert any(r.get("replace_original") is True for r in tr.responses)     # the plan message updated in place


def test_non_review_interactions_are_ignored(tmp_path):
    svc, _ = service(tmp_path)
    assert not slack_app.handle_interaction({"type": "view_submission"}, svc, FakeTransport(), None, NOW)["handled"]
    other = {"type": "block_actions", "actions": [{"action_id": "something:else", "value": "{}"}]}
    assert not slack_app.handle_interaction(other, svc, FakeTransport(), None, NOW)["handled"]


def test_tokens_are_never_logged(monkeypatch):
    class S:
        slack_bot_token, slack_channel, slack_webhook_url = "xoxb-SECRET-TOKEN", "C1", ""

    class R:
        ok, status_code = True, 200

        def json(self):
            return {"ok": False, "error": "invalid_auth"}
    monkeypatch.setattr(slack.requests, "post", lambda *a, **k: R())
    lines = []
    slack.Transport(S(), log=lines.append).post({"text": "x"})
    assert lines and not any("SECRET" in line for line in lines)
