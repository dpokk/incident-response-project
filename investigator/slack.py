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

    def set_item(self, incident_id: str, field: str, key: str, value) -> dict:
        """Atomically set one item of a nested map (e.g. executions[execution_id] = message ts)."""
        with self._lock:
            data = self._load()
            entry = data.setdefault(incident_id, {})
            entry[field] = {**(entry.get(field) or {}), key: value}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            return entry


# --------------------------------------------------------------------------- the incident thread
#
# `execution` (Iteration 7) is the execution service when this process can execute, else None. It is used only to
# READ execution records and the policy's max plan age for display (duck-typed: this module does not import it).

def base_key(key: str) -> str:
    """The incident a review key belongs to: a rollback plan (<incident>#rollback-<id>) lives in its thread."""
    return key.split(view.ROLLBACK_MARK)[0]


def _slot(registry: ThreadRegistry, key: str) -> tuple[dict, dict]:
    """(the incident's thread entry, {"plan_ts", "digest"} of the plan message for this review key)."""
    entry = registry.get(base_key(key))
    if key == base_key(key):
        return entry, {"plan_ts": entry.get("plan_ts"), "digest": entry.get("digest")}
    return entry, (entry.get("rollbacks") or {}).get(key, {})


def _stale_after(execution, stale_after_s):
    if stale_after_s is not None:
        return stale_after_s
    return execution.policy.max_plan_age_s if execution is not None else view.STALE_AFTER_S


def _plan_payload(review, key: str, digest: str, now: float, execution=None, stale_after_s=None) -> dict | None:
    rec = review.plan(key, digest)
    if rec is None:
        return None
    statuses = review.statuses(key, digest)
    execs = {}
    if execution is not None:
        execs = {i: r for i in statuses if (r := execution.store.for_action(key, digest, i))}
    return view.plan_message(rec["plan"], digest, statuses, rec["collected_at"], now,
                             stale_after_s=_stale_after(execution, stale_after_s),
                             executable=execution is not None and getattr(execution, "actuator", None) is not None,
                             executions=execs)


def publish_detection(transport: Transport, registry: ThreadRegistry, incident: dict, namespace: str) -> None:
    signals = [s["text"] for s in incident.get("signals", [])]
    res = transport.post(view.thread_root(incident["id"], namespace, signals=signals))
    if res is not None:
        registry.set(incident["id"], channel=res[0], root_ts=res[1], namespace=namespace, signals=signals)


def publish_investigation(transport: Transport, registry: ThreadRegistry, report: dict, review, digest: str,
                          namespace: str, now: float, execution=None, stale_after_s=None) -> bool:
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
    res = transport.post(_plan_payload(review, iid, digest, now, execution, stale_after_s), thread_ts=root)
    if res is None:
        return False
    registry.set(iid, plan_ts=res[1], digest=digest, headline=headline, namespace=namespace)
    refresh_root(transport, registry, review, iid, execution)
    return True


def refresh_plan(transport: Transport, registry: ThreadRegistry, review, incident_id: str, digest: str, now: float,
                 response_url: str | None = None, execution=None, stale_after_s=None) -> bool:
    """Re-render a plan message (an incident's plan or a rollback plan) from the stored plan, the recorded decisions
    and, in Iteration 7, the execution records."""
    payload = _plan_payload(review, incident_id, digest, now, execution, stale_after_s)
    if payload is None:
        return False
    entry, slot = _slot(registry, incident_id)
    if slot.get("plan_ts") and slot.get("digest") == digest \
            and transport.update(entry.get("channel"), slot["plan_ts"], payload):
        return True
    if response_url:
        return transport.respond(response_url, {**payload, "replace_original": True})
    return False


def _execution_summary(registry: ThreadRegistry, incident_id: str, execution) -> str | None:
    if execution is None:
        return None
    entry = registry.get(incident_id)
    parts = []
    for key in [incident_id] + list((entry.get("rollbacks") or {}).keys()):
        for r in execution.store.executions(key):
            what = "rollback" if key != incident_id else f"action {r['action_index'] + 1}"
            parts.append(f"{what} → {r['outcome'] or r['status']}")
    return "; ".join(parts) or None


def refresh_root(transport: Transport, registry: ThreadRegistry, review, incident_id: str, execution=None) -> bool:
    """Re-render the root from the registry, so every refresh (e.g. after a review click) keeps what is known:
    the diagnosis, the review summary, the execution outcome and whether the symptoms have cleared."""
    incident_id = base_key(incident_id)
    entry = registry.get(incident_id)
    if not entry.get("root_ts"):
        return False
    return transport.update(entry.get("channel"), entry["root_ts"], view.thread_root(
        incident_id, entry.get("namespace", ""), signals=entry.get("signals"), headline=entry.get("headline"),
        review_summary=review.summary(incident_id) if entry.get("digest") else None,
        resolved=bool(entry.get("resolved")), execution_summary=_execution_summary(registry, incident_id, execution)))


def publish_decision(transport: Transport, registry: ThreadRegistry, record: dict, plan: dict,
                     response_url: str | None = None) -> bool:
    entry = registry.get(base_key(record["incident_id"]))
    payload = view.decision_message(record, plan)
    if entry.get("root_ts") and transport.threaded:
        return transport.post(payload, thread_ts=entry["root_ts"]) is not None
    if response_url:
        return transport.respond(response_url, {**payload, "replace_original": False, "response_type": "in_channel"})
    return transport.post(payload) is not None


def publish_resolved(transport: Transport, registry: ThreadRegistry, review, incident: dict, namespace: str,
                     execution=None) -> None:
    entry = registry.get(incident["id"])
    payload = view.thread_root(incident["id"], namespace, headline=entry.get("headline"), resolved=True)
    if entry.get("root_ts"):
        registry.set(incident["id"], resolved=True)     # remembered, so later refreshes keep "symptoms have cleared"
        transport.post(payload, thread_ts=entry["root_ts"])
        refresh_root(transport, registry, review, incident["id"], execution)
    else:
        transport.post(payload)


# --------------------------------------------------------------------------- execution in the thread (Iteration 7)

def publish_execution(transport: Transport, registry: ThreadRegistry, key: str, execution_id: str, payload: dict,
                      response_url: str | None = None) -> bool:
    """Post the execution message once, then update the same message at each stage."""
    base = base_key(key)
    entry = registry.get(base)
    ts = (entry.get("executions") or {}).get(execution_id)
    if ts and transport.update(entry.get("channel"), ts, payload):
        return True
    if entry.get("root_ts") and transport.threaded:
        res = transport.post(payload, thread_ts=entry["root_ts"])
        if res is not None:
            registry.set_item(base, "executions", execution_id, res[1])
            return True
        return False
    if response_url:
        return transport.respond(response_url, {**payload, "replace_original": False, "response_type": "in_channel"})
    return transport.post(payload) is not None


def publish_execution_refused(transport: Transport, registry: ThreadRegistry, key: str, payload: dict,
                              response_url: str | None = None) -> bool:
    entry = registry.get(base_key(key))
    if entry.get("root_ts") and transport.threaded:
        return transport.post(payload, thread_ts=entry["root_ts"]) is not None
    if response_url:
        return transport.respond(response_url, {**payload, "replace_original": False, "response_type": "in_channel"})
    return transport.post(payload) is not None


def publish_rollback_plan(transport: Transport, registry: ThreadRegistry, review, key: str, digest: str, now: float,
                          execution=None, response_url: str | None = None) -> bool:
    """The rollback plan, in the incident's thread, with its own review controls (it is never executed here)."""
    payload = _plan_payload(review, key, digest, now, execution)
    if payload is None:
        return False
    base = base_key(key)
    entry = registry.get(base)
    if entry.get("root_ts") and transport.threaded:
        res = transport.post(payload, thread_ts=entry["root_ts"])
        if res is None:
            return False
        registry.set_item(base, "rollbacks", key, {"plan_ts": res[1], "digest": digest})
        return True
    if response_url:
        return transport.respond(response_url, {**payload, "replace_original": False, "response_type": "in_channel"})
    return transport.post(payload) is not None


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
