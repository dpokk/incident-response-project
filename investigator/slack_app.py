"""Inbound Slack interactions over Socket Mode.

    Iteration 6  Approve / Reject / Investigate first / Acknowledge -> review.ReviewService.decide() -> recorded
    Iteration 7  Execute -> the execution service it is GIVEN (never imported here) -> thread updated per stage

This module only translates a Slack interaction into a call and shows the result. It contains no remediation or
execution logic and cannot reach any infrastructure: an Execute click becomes an ExecutionRequest handed to the
service the composition root passed in, which performs every safety check itself. Socket Mode keeps an outbound
WebSocket to Slack, so no public URL is needed. The app token is never logged.
"""
import json
import threading
import time

from . import slack
from . import slack_view as view
from .execution_model import ExecutionRequest, Refusal

PRIVATE_REFUSALS = {Refusal.UNAUTHORIZED.value, Refusal.ALREADY_COMPLETED.value, Refusal.IN_PROGRESS.value}


def handle_interaction(payload: dict, review, transport: "slack.Transport", registry: "slack.ThreadRegistry",
                       now: float, log=print, execution=None, run_async: bool = True) -> dict:
    """Handle one block_actions interaction from a plan message. Returns what happened (also used by tests)."""
    if payload.get("type") != "block_actions" or not payload.get("actions"):
        return {"handled": False, "reason": "not a block action"}
    action = payload["actions"][0]
    parts = str(action.get("action_id", "")).split(":")
    if len(parts) == 2 and parts[0] == "exec":
        return _execute(payload, action, int(parts[1]), review, transport, registry, now, log, execution, run_async)
    if len(parts) != 3 or parts[0] != "review":
        return {"handled": False, "reason": "not a review control"}
    decision, index = parts[1], int(parts[2])
    ref = json.loads(action["value"])
    reviewer = (payload.get("user") or {}).get("id", "")
    state = ((payload.get("state") or {}).get("values") or {}).get(f"param:{index}", {})
    supplied = (state.get("value") or {}).get("value")
    response_url = payload.get("response_url")

    outcome = review.decide(ref["incident"], ref["digest"], index, decision, reviewer, supplied=supplied)
    if outcome.effective:
        rec = review.plan(ref["incident"], ref["digest"])
        slack.refresh_plan(transport, registry, review, ref["incident"], ref["digest"], now, response_url,
                           execution=execution)
        slack.publish_decision(transport, registry, outcome.record, rec["plan"], response_url)
        slack.refresh_root(transport, registry, review, ref["incident"], execution)
        log(f"review: {outcome.record['decision']} on {ref['incident']} action {index + 1} by {reviewer} "
            f"(plan {ref['digest'][:12]}); nothing executed")
    else:
        if response_url:   # only the person who clicked sees why nothing was recorded
            transport.respond(response_url, {"response_type": "ephemeral", "replace_original": False,
                                             "text": outcome.message})
        if outcome.status == "superseded":
            slack.refresh_plan(transport, registry, review, ref["incident"], ref["digest"], now, response_url,
                               execution=execution)
        log(f"review: refused {decision} on {ref['incident']} action {index + 1} by {reviewer}: {outcome.message}")
    return {"handled": True, "effective": outcome.effective, "status": outcome.status, "message": outcome.message,
            "record": outcome.record}


# --------------------------------------------------------------------------- Execute (Iteration 7)

def _execute(payload, action, index, review, transport, registry, now, log, execution, run_async) -> dict:
    ref = json.loads(action["value"])
    key, digest = ref["incident"], ref["digest"]
    user = (payload.get("user") or {}).get("id", "")
    response_url = payload.get("response_url")
    if execution is None:
        if response_url:
            transport.respond(response_url, {"response_type": "ephemeral", "replace_original": False,
                                             "text": "Execution is not available in this listener; nothing was done."})
        return {"handled": True, "started": False, "reason": "no execution service"}
    req = ExecutionRequest(key, digest, index, user, now)
    rec = review.plan(key, digest)
    plan = rec["plan"] if rec else None
    out: dict = {"handled": True, "started": True}

    def job():
        state = {"checks": None, "window": None, "eid": None}

        def progress(stage: str, data: dict) -> None:
            eid = data.get("execution_id")
            state["eid"] = eid
            if stage == "checks_passed":
                state["checks"] = data.get("checks")
            elif stage in ("applied", "stopped") and data.get("result") is not None:
                state["checks"] = data["result"].checks or state["checks"]
            elif stage == "verifying":
                state["window"] = data
            if plan is None or eid is None:
                return
            shown = "checks" if stage == "checks_passed" else stage
            slack.publish_execution(transport, registry, key, eid, view.execution_message(
                plan, index, user, shown, state["checks"], execution.store.get(eid), state["window"]), response_url)
            if stage in ("checks_passed", "stopped", "completed"):
                slack.refresh_plan(transport, registry, review, key, digest, time.time(), execution=execution)
                slack.refresh_root(transport, registry, review, key, execution)

        try:
            result = execution.execute(req, progress)
        except Exception as exc:  # noqa: BLE001 - reported; the executor records its own state
            log(f"execute: {type(exc).__name__}: {exc}")
            result = None
        out["result"] = result
        if result is None:
            return
        if state["eid"] is None:            # refused before anything was claimed or run
            if result.code in PRIVATE_REFUSALS:
                if response_url:
                    transport.respond(response_url, {"response_type": "ephemeral", "replace_original": False,
                                                     "text": result.message})
            else:
                slack.publish_execution_refused(transport, registry, key, view.execution_refused_message(
                    plan, index, user, result.message, result.checks), response_url)
            log(f"execute: refused {result.code} on {key} action {index + 1} by {user}")
            return
        rb = ((result.record or {}).get("verification") or {}).get("rollback") or {}
        if rb.get("available"):
            slack.publish_rollback_plan(transport, registry, review, rb["incident_key"], rb["digest"], time.time(),
                                        execution, response_url)
        log(f"execute: {result.code} on {key} action {index + 1} by {user}")

    if run_async:
        threading.Thread(target=job, name=f"execute-{key}-{index}", daemon=True).start()
    else:
        job()
    return out


def start(settings, review, log=print, block: bool = False, execution=None):
    """Connect to Slack over Socket Mode and handle review (and, if `execution` is given, Execute) interactions."""
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse

    if not settings.slack_app_token:
        raise RuntimeError("SLACK_APP_TOKEN is not set: Socket Mode (interactive review) is unavailable")
    transport = slack.Transport(settings, log)
    registry = slack.ThreadRegistry(settings.slack_threads_path)
    client = SocketModeClient(app_token=settings.slack_app_token)

    def listener(c, req):
        c.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))   # acknowledge within 3 s
        if req.type != "interactive":
            return
        try:
            handle_interaction(req.payload, review, transport, registry, time.time(), log, execution=execution)
        except Exception as exc:  # noqa: BLE001 - one bad interaction must not stop the listener
            log(f"review interaction failed: {type(exc).__name__}: {exc}")

    client.socket_mode_request_listeners.append(listener)
    client.connect()
    log(f"Slack review listener connected (Socket Mode); {len(review.approvers)} approver(s) configured"
        + ("; Execute enabled (a separate step after approval)" if execution is not None else "; Execute not available"))
    if block:
        threading.Event().wait()
    return client
