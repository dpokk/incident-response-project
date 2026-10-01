"""Iteration 7, Milestone 4: the execution lifecycle in the Slack incident thread, and scoped suppression of the
expected effects of a change. Fake transport, fake cluster, fake actuator; nothing reaches Slack or a cluster."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fake_cluster as fc  # noqa: E402
from test_execution import APPROVER, EXECUTOR, make_service  # noqa: E402
from test_execution_apply import with_limit  # noqa: E402
from test_slack_review import OTHER, FakeTransport, buttons, make_plan, text_of  # noqa: E402
from test_verification import SwitchingActuator, World, oom_still_failing  # noqa: E402

from investigator import slack, slack_app, slack_view  # noqa: E402
from investigator.review import plan_digest  # noqa: E402

NOW = fc.NOW


def thread(tmp_path, after=None, approve=("256Mi",)):
    """An incident thread (detection + investigation + plan) with action 1 approved, and an execution service."""
    svc, review, d, plan, _, clock = make_service(tmp_path, world=fc.world_oom(), approve=approve)
    world = World(fc.world_oom(), after or with_limit(fc.base_world(), "256Mi"))
    svc.actuator, svc.capabilities = SwitchingActuator(world), world.caps
    _, report = make_plan()
    report["id"] = "INC-TEST"
    tr, reg = FakeTransport(), slack.ThreadRegistry(tmp_path / "threads.json")
    slack.publish_detection(tr, reg, {"id": "INC-TEST", "signals": [{"text": "restarts"}]}, "shop")
    assert slack.publish_investigation(tr, reg, report, review, d, "shop", NOW, execution=svc)
    return svc, review, d, tr, reg, clock


def click(action_id, key, digest, index, user=EXECUTOR):
    return {"type": "block_actions", "user": {"id": user}, "response_url": "https://hooks.slack.test/r",
            "actions": [{"action_id": action_id,
                         "value": json.dumps({"incident": key, "digest": digest, "action": index})}],
            "state": {"values": {}}}


def execute(svc, review, tr, reg, key, digest, index=0, user=EXECUTOR):
    return slack_app.handle_interaction(click(f"exec:{index}", key, digest, index, user), review, tr, reg, NOW,
                                        log=lambda *_: None, execution=svc, run_async=False)


def last_update(tr, ts):
    return [p for t, p in tr.updates if t == ts][-1]


# --------------------------------------------------------------------------- rendering

def test_execute_is_offered_only_on_approved_change_actions_where_execution_is_available():
    plan, _ = make_plan()
    d = plan_digest(plan)
    approved = {0: {"status": "approved", "reviewer": APPROVER, "at": NOW, "supplied_parameters": {"proposed_limit": "256Mi"}},
                1: {"status": "acknowledged", "reviewer": APPROVER, "at": NOW}}
    assert buttons(slack_view.plan_message(plan, d, approved, NOW, NOW, executable=True)) == ["exec:0"]
    assert buttons(slack_view.plan_message(plan, d, approved, NOW, NOW)) == []                  # no executor here
    assert "exec:0" not in buttons(slack_view.plan_message(plan, d, {}, NOW, NOW, executable=True))   # not approved
    stale = slack_view.plan_message(plan, d, approved, NOW - 1000, NOW, executable=True, stale_after_s=900)
    assert buttons(stale) == [] and "older than the 15 min the execution policy allows" in text_of(stale)
    assert ":warning:" not in text_of(slack_view.plan_message(plan, d, approved, NOW - 1000, NOW, stale_after_s=1800))
    done = slack_view.plan_message(plan, d, approved, NOW, NOW, executable=True,
                                   executions={0: {"status": "completed", "outcome": "RESOLVED", "executor": EXECUTOR}})
    assert buttons(done) == [] and "executed — RESOLVED" in text_of(done)


def test_rollback_plans_are_labelled_as_such():
    plan, _ = make_plan()
    plan["incident_id"] = "INC-TEST#rollback-abc"
    assert "Rollback plan — proposed, not executed" in text_of(slack_view.plan_message(plan, "d" * 64, {}, NOW, NOW))


# --------------------------------------------------------------------------- the lifecycle in the thread

def test_execute_shows_checks_progress_evidence_and_outcome_in_one_message(tmp_path):
    svc, review, d, tr, reg, _ = thread(tmp_path)
    plan_ts, root = reg.get("INC-TEST")["plan_ts"], reg.get("INC-TEST")["root_ts"]
    assert "exec:0" in buttons(last_update(tr, plan_ts) if any(t == plan_ts for t, _ in tr.updates)
                               else tr.posts[2][1])
    posts_before = len(tr.posts)
    out = execute(svc, review, tr, reg, "INC-TEST", d)
    assert out["result"].code == "RESOLVED"
    assert len(tr.posts) == posts_before + 1 and tr.posts[-1][0] == root     # ONE execution message, in the thread
    ts = reg.get("INC-TEST")["executions"][out["result"].execution_id]
    final = text_of(last_update(tr, ts))
    for part in ("Execution result — action 1", f"<@{EXECUTOR}>", f"approved by <@{APPROVER}>", "Safety checks:",
                 "approved by", "plan age", "memory limit is still 192Mi", "Dry run:", "Applied at", "`192Mi` → `256Mi`",
                 "Verification (settled after", "Outcome: RESOLVED"):
        assert part in final, part
    assert "executed — RESOLVED" in text_of(last_update(tr, plan_ts)) and "exec:0" not in buttons(last_update(tr, plan_ts))
    assert "Remediation:* action 1 → RESOLVED" in text_of(last_update(tr, root))
    stages = [text_of(p).split("\n")[0] for t, p in tr.updates if t == ts]
    assert any("Verifying" in s for s in stages)                              # progress shown before the outcome


def test_a_failed_verification_posts_the_outcome_and_a_rollback_plan_that_needs_its_own_controls(tmp_path):
    svc, review, d, tr, reg, _ = thread(tmp_path, after=oom_still_failing())
    out = execute(svc, review, tr, reg, "INC-TEST", d)
    assert out["result"].code == "NOT_RESOLVED"
    ts = reg.get("INC-TEST")["executions"][out["result"].execution_id]
    assert "No further change was made. A rollback plan has been posted below" in text_of(last_update(tr, ts))
    [(key, slot)] = reg.get("INC-TEST")["rollbacks"].items()
    rb_msg = tr.posts[-1][1]
    assert "Rollback plan — proposed, not executed" in text_of(rb_msg)
    assert buttons(rb_msg) == ["review:approved:0", "review:rejected:0", "review:investigate_first:0"]   # review first
    calls = len(svc.actuator.calls)
    assert execute(svc, review, tr, reg, key, slot["digest"])["result"].code == "EXECUTION_REFUSED_NOT_APPROVED"
    assert len(svc.actuator.calls) == calls                                   # nothing without approval
    slack_app.handle_interaction(click("review:approved:0", key, slot["digest"], 0, APPROVER), review, tr, reg, NOW,
                                 log=lambda *_: None, execution=svc)
    assert buttons(last_update(tr, slot["plan_ts"])) == ["exec:0"]            # approved -> Execute offered
    svc.actuator.world.after = with_limit(fc.world_oom(), "192Mi")
    back = execute(svc, review, tr, reg, key, slot["digest"])
    assert back["result"].ok and [dry for _, dry, _ in svc.actuator.calls[calls:]] == [True, False]
    assert "rollback →" in text_of(last_update(tr, reg.get("INC-TEST")["root_ts"]))


def test_refusals_are_explained_and_private_ones_only_to_the_clicker(tmp_path):
    svc, review, d, tr, reg, clock = thread(tmp_path)
    posts = len(tr.posts)
    execute(svc, review, tr, reg, "INC-TEST", d, user=OTHER)                  # not an executor
    assert len(tr.posts) == posts and "not an authorised executor" in tr.responses[-1]["text"]
    assert tr.responses[-1]["response_type"] == "ephemeral"
    clock.t = NOW + 3600                                                       # plan now older than the policy allows
    execute(svc, review, tr, reg, "INC-TEST", d)
    msg = text_of(tr.posts[-1][1])
    assert "Execution refused" in msg and "fresh investigation required" in msg and svc.actuator.calls == []


def test_a_refusal_after_the_live_recheck_is_shown_in_the_execution_message(tmp_path):
    svc, review, d, tr, reg, _ = thread(tmp_path)
    svc.actuator.world.current = with_limit(fc.world_oom(), "384Mi")           # someone changed it meanwhile
    out = execute(svc, review, tr, reg, "INC-TEST", d)
    assert out["result"].code == "EXECUTION_REFUSED_STATE_CHANGED"
    text = text_of(last_update(tr, reg.get("INC-TEST")["executions"][out["result"].execution_id]))
    assert "execution refused" in text and "384Mi" in text and svc.actuator.calls == []


def test_duplicate_clicks_never_execute_twice_and_are_answered_privately(tmp_path):
    svc, review, d, tr, reg, _ = thread(tmp_path)
    execute(svc, review, tr, reg, "INC-TEST", d)
    posts = len(tr.posts)
    execute(svc, review, tr, reg, "INC-TEST", d)
    assert len(tr.posts) == posts and "not executed again" in tr.responses[-1]["text"]
    assert sum(1 for _, dry, _ in svc.actuator.calls if not dry) == 1


def test_without_an_execution_service_execute_is_not_available(tmp_path):
    svc, review, d, tr, reg, _ = thread(tmp_path)
    out = slack_app.handle_interaction(click("exec:0", "INC-TEST", d, 0), review, tr, reg, NOW, log=lambda *_: None)
    assert out["started"] is False and "not available" in tr.responses[-1]["text"]


# --------------------------------------------------------------------------- scoped suppression of expected effects

def test_expected_effects_are_scoped_to_the_incident_components_and_the_execution_period(tmp_path):
    svc, review, d, tr, reg, clock = thread(tmp_path)
    seen = []

    def watch_during(stage, data):          # what detection would ask while the change is being verified
        if stage == "observing":
            seen.append((svc.expected_effect("backend", clock()), svc.expected_effect("frontend", clock()),
                         svc.expected_effect("postgres", clock())))
    svc.execute(slack_app.ExecutionRequest("INC-TEST", d, 0, EXECUTOR, NOW), watch_during)
    assert seen == [("INC-TEST", "INC-TEST", None)]       # backend (changed) and frontend (impacted); not postgres
    assert svc.expected_effect("backend", clock() + 30) == "INC-TEST"          # short grace after verification
    assert svc.expected_effect("backend", clock() + 61) is None                # then detection is fully back


def test_a_change_that_never_happened_suppresses_nothing(tmp_path):
    svc, review, d, tr, reg, clock = thread(tmp_path)
    svc.actuator.world.current = with_limit(fc.world_oom(), "384Mi")
    execute(svc, review, tr, reg, "INC-TEST", d)
    assert svc.expected_effect("backend", clock()) is None


def test_detection_drops_only_the_expected_effects():
    from investigator.pipeline import without_expected_effects

    class Svc:
        def expected_effect(self, component, t):
            return "INC-TEST" if component == "backend" and t < 100 else None
    sigs = [{"kind": "process_restart", "subject": "backend", "text": "x"},
            {"kind": "process_restart", "subject": "postgres", "text": "y"}]
    logged = set()
    assert [x["subject"] for x in without_expected_effects(sigs, Svc(), 50, logged)] == ["postgres"]  # unrelated: kept
    assert [x["subject"] for x in without_expected_effects(sigs, Svc(), 150, logged)] == ["backend", "postgres"]
    assert without_expected_effects(sigs, None, 50, logged) == sigs
