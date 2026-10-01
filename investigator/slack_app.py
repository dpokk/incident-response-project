"""Inbound Slack interactions over Socket Mode (Iteration 6): Approve / Reject / Investigate first / Acknowledge.

    Slack button click -> handle_interaction() -> review.ReviewService.decide() -> durable decision
                                              -> thread updated (slack.py) -> STOP

This module only translates a Slack interaction into a review decision and shows the result. It contains no
remediation logic, cannot reach any infrastructure, and nothing it does executes a change. Socket Mode keeps an
outbound WebSocket to Slack, so no public URL is needed. The app token is never logged.
"""
import json
import threading
import time

from . import slack


def handle_interaction(payload: dict, review, transport: "slack.Transport", registry: "slack.ThreadRegistry",
                       now: float, log=print) -> dict:
    """Handle one block_actions interaction from a plan message. Returns what happened (also used by tests)."""
    if payload.get("type") != "block_actions" or not payload.get("actions"):
        return {"handled": False, "reason": "not a block action"}
    action = payload["actions"][0]
    parts = str(action.get("action_id", "")).split(":")
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
        slack.refresh_plan(transport, registry, review, ref["incident"], ref["digest"], now, response_url)
        slack.publish_decision(transport, registry, outcome.record, rec["plan"], response_url)
        slack.refresh_root(transport, registry, review, ref["incident"])
        log(f"review: {outcome.record['decision']} on {ref['incident']} action {index + 1} by {reviewer} "
            f"(plan {ref['digest'][:12]}); nothing executed")
    else:
        if response_url:   # only the person who clicked sees why nothing was recorded
            transport.respond(response_url, {"response_type": "ephemeral", "replace_original": False,
                                             "text": outcome.message})
        if outcome.status == "superseded":
            slack.refresh_plan(transport, registry, review, ref["incident"], ref["digest"], now, response_url)
        log(f"review: refused {decision} on {ref['incident']} action {index + 1} by {reviewer}: {outcome.message}")
    return {"handled": True, "effective": outcome.effective, "status": outcome.status, "message": outcome.message,
            "record": outcome.record}


def start(settings, review, log=print, block: bool = False):
    """Connect to Slack over Socket Mode and handle review interactions until stopped."""
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
            handle_interaction(req.payload, review, transport, registry, time.time(), log)
        except Exception as exc:  # noqa: BLE001 - one bad interaction must not stop the listener
            log(f"review interaction failed: {type(exc).__name__}: {exc}")

    client.socket_mode_request_listeners.append(listener)
    client.connect()
    log(f"Slack review listener connected (Socket Mode); {len(review.approvers)} approver(s) configured")
    if block:
        threading.Event().wait()
    return client
