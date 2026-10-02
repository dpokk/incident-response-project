"""The demo's Slack Socket Mode listener. Review and Execute clicks go to the unchanged Iteration 6/7 handler
(`investigator.slack_app.handle_interaction`); the Remediation Agent's own buttons (Stop, Acknowledge) go to the
engine. One connection only: Slack spreads interactions across every open Socket Mode connection of an app."""
import threading
import time

from investigator import slack
from investigator import slack_app

AUTO_PREFIX = "auto:"


def start(settings, review, execution, on_auto, log=print):
    """`on_auto(kind, incident_id, user, response_url)` handles `auto:stop` / `auto:ack` and returns a message for the
    person who clicked (shown only to them), or None."""
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse

    if not settings.slack_app_token:
        raise RuntimeError("SLACK_APP_TOKEN is not set: Socket Mode (interactive review) is unavailable")
    transport = slack.Transport(settings, log)
    registry = slack.ThreadRegistry(settings.slack_threads_path)
    client = SocketModeClient(app_token=settings.slack_app_token)

    def handle(payload: dict) -> None:
        actions = payload.get("actions") or []
        aid = str((actions[0] if actions else {}).get("action_id", ""))
        if payload.get("type") == "block_actions" and aid.startswith(AUTO_PREFIX):
            user = (payload.get("user") or {}).get("id", "")
            reply = on_auto(aid[len(AUTO_PREFIX):], actions[0].get("value", ""), user)
            if reply and payload.get("response_url"):
                transport.respond(payload["response_url"], {"response_type": "ephemeral", "replace_original": False,
                                                            "text": reply})
            return
        slack_app.handle_interaction(payload, review, transport, registry, time.time(), log, execution=execution)

    def listener(c, req):
        c.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))   # acknowledge within 3 s
        if req.type != "interactive":
            return
        def run():
            try:
                handle(req.payload)
            except Exception as exc:  # noqa: BLE001 - one bad interaction must not stop the listener
                log(f"Slack interaction failed: {type(exc).__name__}: {exc}")
        threading.Thread(target=run, daemon=True, name="slack-interaction").start()

    client.socket_mode_request_listeners.append(listener)
    client.connect()
    log(f"Slack listener connected (Socket Mode); {len(settings.slack_approvers)} human approver(s); Execute enabled; "
        f"Remediation Agent controls (Stop, Acknowledge) enabled")
    return client
