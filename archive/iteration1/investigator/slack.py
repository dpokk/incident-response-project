"""Slack delivery via a bot token (chat.postMessage) or an incoming webhook."""
import requests


def post(settings, payload: dict, log=print) -> bool:
    if settings.slack_bot_token and settings.slack_channel:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {settings.slack_bot_token}",
                     "Content-Type": "application/json; charset=utf-8"},
            json={"channel": settings.slack_channel, "unfurl_links": False, **payload},
            timeout=15,
        )
        body = r.json() if r.ok else {"ok": False, "error": f"HTTP {r.status_code}"}
        if not body.get("ok"):
            log(f"  ! Slack chat.postMessage failed: {body.get('error')}"
                + (" (invite the bot to the channel: /invite @<bot>)" if body.get("error") == "not_in_channel" else ""))
            return False
        return True
    if settings.slack_webhook_url:
        r = requests.post(settings.slack_webhook_url, json=payload, timeout=15)
        if r.status_code != 200:
            log(f"  ! Slack webhook failed: HTTP {r.status_code} {r.text[:200]}")
            return False
        return True
    log("  Slack not configured (set SLACK_BOT_TOKEN+SLACK_CHANNEL or SLACK_WEBHOOK_URL in .env); skipped")
    return False


def check(settings) -> str:
    if settings.slack_bot_token:
        r = requests.post("https://slack.com/api/auth.test",
                          headers={"Authorization": f"Bearer {settings.slack_bot_token}"}, timeout=10).json()
        return f"bot token OK (team={r.get('team')}, bot={r.get('user')})" if r.get("ok") else f"bot token error: {r.get('error')}"
    if settings.slack_webhook_url:
        return "webhook configured (not tested to avoid posting)"
    return "not configured"
