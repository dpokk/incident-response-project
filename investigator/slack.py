"""Slack transport and the incident thread (Iteration 6).

Outbound only - inbound interactions arrive through slack_app.py (Socket Mode).

  * With a bot token + channel: chat.postMessage returns a message timestamp, so each incident gets one thread
    (root message + replies) and messages can be updated (chat.update) as review decisions arrive.
  * With only the incoming webhook: the same rendered messages are posted, unthreaded, and cannot be updated
    (a webhook returns no message id). Interactive updates then go through the interaction's response_url.

Rendering is slack_view.py's job; decisions are review.py's. Tokens are never logged.
"""
import json
import os
import threading
from pathlib import Path

import requests

from . import slack_view as view

API = "https://slack.com/api"


class Transport:
    def __init__(self, settings, log=print):
        self.s, self.log = settings, log

    @property
    def threaded(self) -> bool:
        return bool(self.s.slack_bot_token and self.s.slack_channel)

    @property
    def configured(self) -> bool:
        return self.threaded or bool(self.s.slack_webhook_url)

    def _api(self, method: str, body: dict) -> dict:
        r = requests.post(f"{API}/{method}", headers={"Authorization": f"Bearer {self.s.slack_bot_token}",
                                                      "Content-Type": "application/json; charset=utf-8"},
                          json=body, timeout=15)
        out = r.json() if r.ok else {"ok": False, "error": f"HTTP {r.status_code}"}
        if not out.get("ok"):
            self.log(f"  ! Slack {method} failed: {out.get('error')}"
                     + (" (invite the bot to the channel: /invite @<bot>)" if out.get("error") == "not_in_channel" else ""))
        return out

    def post(self, payload: dict, thread_ts: str | None = None) -> tuple[str | None, str | None] | None:
        """Post a message; returns (channel, ts) when threaded, (None, None) for a webhook post, None on failure."""
        if self.threaded:
            body = {"channel": self.s.slack_channel, "unfurl_links": False, **payload}
            if thread_ts:
                body["thread_ts"] = thread_ts
            out = self._api("chat.postMessage", body)
            return (out["channel"], out["ts"]) if out.get("ok") else None
        if self.s.slack_webhook_url:
            r = requests.post(self.s.slack_webhook_url, json=payload, timeout=15)
            if r.status_code != 200:
                self.log(f"  ! Slack webhook failed: HTTP {r.status_code} {r.text[:200]}")
                return None
            return (None, None)
        self.log("  Slack not configured (set SLACK_BOT_TOKEN+SLACK_CHANNEL or SLACK_WEBHOOK_URL in .env); skipped")
        return None

    def update(self, channel: str | None, ts: str | None, payload: dict) -> bool:
        if not (self.threaded and channel and ts):
            return False
        return bool(self._api("chat.update", {"channel": channel, "ts": ts, **payload}).get("ok"))

    def respond(self, response_url: str, payload: dict) -> bool:
        """Reply through an interaction's response_url (works without a bot token, e.g. for webhook messages)."""
        try:
            return requests.post(response_url, json=payload, timeout=15).status_code == 200
        except requests.RequestException as exc:
            self.log(f"  ! Slack response_url failed: {type(exc).__name__}")
            return False


class ThreadRegistry:
    """incident_id -> {"channel", "root_ts", "plan_ts", "digest", "namespace", "headline", "signals"}
    (state/slack_threads.json). Slack-side bookkeeping only; review decisions live in review.py's store."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def get(self, incident_id: str) -> dict:
        with self._lock:
            return self._load().get(incident_id, {})

    def set(self, incident_id: str, **fields) -> dict:
        with self._lock:
            data = self._load()
            entry = {**data.get(incident_id, {}), **fields}
            data[incident_id] = entry
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            return entry


# --------------------------------------------------------------------------- the incident thread

def publish_detection(transport: Transport, registry: ThreadRegistry, incident: dict, namespace: str) -> None:
    signals = [s["text"] for s in incident.get("signals", [])]
    res = transport.post(view.thread_root(incident["id"], namespace, signals=signals))
    if res is not None:
        registry.set(incident["id"], channel=res[0], root_ts=res[1], namespace=namespace, signals=signals)


def publish_investigation(transport: Transport, registry: ThreadRegistry, report: dict, review, digest: str,
                          namespace: str, now: float) -> bool:
    """Investigation summary and the plan for review, in the incident's thread (started here if detection was not
    posted). The plan message carries the review controls and is updated as decisions are recorded."""
    iid = report["id"]
    entry = registry.get(iid)
    headline = f"{report['failure_category_label']} in {report['affected_component']['name']}"
    if not entry.get("root_ts") and transport.threaded:
        res = transport.post(view.thread_root(iid, namespace, headline=headline, review_summary=review.summary(iid)))
        if res is None:
            return False
        entry = registry.set(iid, channel=res[0], root_ts=res[1], namespace=namespace)
    root = entry.get("root_ts")
    if transport.post(view.investigation_message(report), thread_ts=root) is None:
        return False
    plan = report["remediation_plan"]
    collected_at = (report.get("window") or {}).get("end")
    res = transport.post(view.plan_message(plan, digest, review.statuses(iid, digest), collected_at, now),
                         thread_ts=root)
    if res is None:
        return False
    registry.set(iid, plan_ts=res[1], digest=digest, headline=headline, namespace=namespace)
    refresh_root(transport, registry, review, iid)
    return True


def refresh_plan(transport: Transport, registry: ThreadRegistry, review, incident_id: str, digest: str, now: float,
                 response_url: str | None = None) -> bool:
    """Re-render the plan message from the stored plan and the recorded decisions."""
    rec = review.plan(incident_id, digest)
    if rec is None:
        return False
    payload = view.plan_message(rec["plan"], digest, review.statuses(incident_id, digest), rec["collected_at"], now)
    entry = registry.get(incident_id)
    if entry.get("plan_ts") and entry.get("digest") == digest \
            and transport.update(entry.get("channel"), entry["plan_ts"], payload):
        return True
    if response_url:
        return transport.respond(response_url, {**payload, "replace_original": True})
    return False


def refresh_root(transport: Transport, registry: ThreadRegistry, review, incident_id: str,
                 resolved: bool = False) -> bool:
    entry = registry.get(incident_id)
    if not entry.get("root_ts"):
        return False
    return transport.update(entry.get("channel"), entry["root_ts"], view.thread_root(
        incident_id, entry.get("namespace", ""), signals=entry.get("signals"), headline=entry.get("headline"),
        review_summary=review.summary(incident_id) if entry.get("digest") else None, resolved=resolved))


def publish_decision(transport: Transport, registry: ThreadRegistry, record: dict, plan: dict,
                     response_url: str | None = None) -> bool:
    entry = registry.get(record["incident_id"])
    payload = view.decision_message(record, plan)
    if entry.get("root_ts") and transport.threaded:
        return transport.post(payload, thread_ts=entry["root_ts"]) is not None
    if response_url:
        return transport.respond(response_url, {**payload, "replace_original": False, "response_type": "in_channel"})
    return transport.post(payload) is not None


def publish_resolved(transport: Transport, registry: ThreadRegistry, review, incident: dict, namespace: str) -> None:
    entry = registry.get(incident["id"])
    payload = view.thread_root(incident["id"], namespace, resolved=True)
    if entry.get("root_ts"):
        transport.post(payload, thread_ts=entry["root_ts"])
        refresh_root(transport, registry, review, incident["id"], resolved=True)
    else:
        transport.post(payload)


# --------------------------------------------------------------------------- single messages and checks

def post(settings, payload: dict, log=print) -> bool:
    """Single, unthreaded message (e.g. `python -m investigator post`)."""
    return Transport(settings, log).post(payload) is not None


def check(settings) -> str:
    out = []
    if settings.slack_bot_token:
        r = requests.post(f"{API}/auth.test", headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
                          timeout=10).json()
        out.append(f"bot token OK (team={r.get('team')}, bot={r.get('user')})" if r.get("ok")
                   else f"bot token error: {r.get('error')}")
    if settings.slack_webhook_url:
        out.append("webhook configured (not tested to avoid posting)")
    out.append("Socket Mode app token " + ("set" if settings.slack_app_token else "not set")
               + f"; {len(settings.slack_approvers)} approver(s) configured")
    return "; ".join(out)
