"""Run one demo incident end to end against the running demo (port 8800) and record its readings.

usage: run_incident.py <key>            reset if needed, trigger, follow until a stop point
       run_incident.py <key> --resume   only follow (after a human approved/executed in Slack)
Stop points: a terminal phase, or awaiting_approval / approved (a human must act in Slack).
Readings are written to readings/<key>.json next to this script and summarised on stdout.
"""
import json
import sys
import time
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8800"
OUT = Path(__file__).parent / "readings"
INJECT: dict = {}
TERMINAL = {"resolved", "not_resolved", "rejected", "failed", "handed_over", "acknowledged", "investigated"}
HUMAN = {"awaiting_approval", "approved"}


def snap():
    return requests.get(f"{BASE}/api/snapshot", timeout=10).json()


def wait_phase(ok, timeout, every=3):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = snap()
            if ok(s):
                return s
        except requests.RequestException:
            pass
        time.sleep(every)
    return None


def events(seconds=4):
    out = []
    try:
        with requests.get(f"{BASE}/api/events", stream=True, timeout=(5, seconds)) as r:
            t0 = time.time()
            for line in r.iter_lines(decode_unicode=True):
                if line and line.startswith("data: "):
                    out.append(json.loads(line[6:]))
                if time.time() - t0 > seconds:
                    break
    except requests.RequestException:
        pass
    return out


def readings(key, evs, s, started):
    inc = s.get("incident") or {}
    last = {}
    phases, reviewer_actor, agent_err, reviewer_err, slack = [], None, None, None, []
    for e in evs:
        t, d = e["type"], e["data"]
        last[t] = d
        if t == "phase":
            phases.append((round(e["t"] - started), d["phase"]))
        elif t == "actor" and d["name"] == "reviewer":
            reviewer_actor = d["text"]
        elif t == "agent_error":
            agent_err = d.get("error")
        elif t == "reviewer_error":
            reviewer_err = d.get("error")
        elif t == "slack":
            slack.append({"text": d.get("text", "")[:120], "ok": d.get("ok")})
    rule, route, ai, rv = last.get("rule_result") or {}, last.get("route") or {}, last.get("agent_result"), last.get("review_result")
    gate, prop = last.get("auto_gate") or {}, last.get("proposal") or {}
    out = last.get("outcome") or {}
    done = last.get("auto_done") or {}
    return {
        "incident": key, "id": inc.get("id"), "final_phase": s["phase"], "phases": phases,
        "rule": {"label": rule.get("label"), "confidence": rule.get("confidence_value"),
                 "root_cause_component": rule.get("root_cause_component"), "root_cause": rule.get("root_cause")},
        "route": {"mode": route.get("mode"), "reason": route.get("reason")},
        "investigator": ({"model": ai.get("model"), "root_cause": ai.get("root_cause"),
                          "component": ai.get("root_cause_component"), "category": ai.get("category"),
                          "fix": ai.get("suggested_fix", {}).get("action"),
                          "value": ai.get("suggested_fix", {}).get("parameter_value"),
                          "agrees_with_rules": bool((ai.get("agreement") or {}).get("category") and
                                                    (ai.get("agreement") or {}).get("component")),
                          "supported": f"{ai['validation']['supported_findings']}/{ai['validation']['findings']}",
                          "tool_calls": ai.get("tool_calls"), "seconds": ai.get("seconds")}
                         if ai else {"error": agent_err}),
        "reviewer": ({"model": rv.get("model"), "verdict": rv.get("verdict"), "effective": rv.get("effective"),
                      "summary": rv.get("summary"),
                      "objections": [{"statement": o["statement"], "supported": o["supported"],
                                      "ids": [x["id"] for x in o["evidence"]]} for o in rv.get("objections", [])],
                      "tool_calls": rv.get("tool_calls"), "seconds": rv.get("seconds")}
                     if rv else {"error": reviewer_err, "actor": reviewer_actor}),
        "gate": {"eligible": gate.get("eligible"),
                 "failed": [c["detail"] for c in gate.get("checks", []) if not c["ok"]]},
        "proposal": {"executable": prop.get("executable"), "action": prop.get("action_type"),
                     "summary": prop.get("summary"), "value": (prop.get("parameter") or {}).get("value"),
                     "source": prop.get("source"), "reason": prop.get("reason")},
        "outcome": {"code": out.get("code"), "message": out.get("message")} if out else None,
        "auto": {"result": done.get("result"), "reason": done.get("reason"),
                 "attempts": [(a.get("value"), a.get("outcome") or a.get("code")) for a in done.get("attempts", [])]}
        if done else None,
        "slack_failures": [x for x in slack if x["ok"] is False],
        "inject_done_s": INJECT.get("done"),
        "elapsed_s": round(time.time() - started),
    }


def summary(r):
    i, v = r["investigator"], r["reviewer"]
    lines = [f"== {r['incident']} ({r['id']}) -> {r['final_phase']} after {r['elapsed_s']} s",
             f"rules: {r['rule']['label']} {r['rule']['confidence']} | route: {r['route']['mode']}",
             (f"investigator: {i.get('component')} / {i.get('fix')} {i.get('value') or ''} | agrees={i.get('agrees_with_rules')} "
              f"supported={i.get('supported')} | {i.get('tool_calls')} calls {i.get('seconds')} s"
              if "error" not in i else f"investigator: NO REPORT ({i['error']})"),
             (f"reviewer: {v.get('effective')} (raw {v.get('verdict')}) | {v.get('tool_calls')} calls {v.get('seconds')} s | "
              f"{(v.get('summary') or '')[:200]}" if "error" not in v else f"reviewer: NO REVIEW ({v.get('error') or v.get('actor')})"),
             f"gate: {r['gate']['eligible']} {r['gate']['failed'][:2]}",
             f"proposal: executable={r['proposal']['executable']} {r['proposal']['summary'] or r['proposal']['reason']}",
             f"outcome: {r['outcome']} auto: {r['auto']}",
             f"slack failures: {r['slack_failures']}"]
    for o in v.get("objections") or []:
        lines.insert(4, f"   objection ({'supported' if o['supported'] else 'unsupported'}): {o['statement'][:160]} {o['ids']}")
    return "\n".join(lines)


def main():
    key, resume = sys.argv[1], "--resume" in sys.argv
    OUT.mkdir(exist_ok=True)
    meta = OUT / f"{key}.start"
    if not resume:
        s = snap()
        if s["phase"] != "monitoring" or s["busy"]:
            requests.post(f"{BASE}/api/reset", timeout=10)
            if not wait_phase(lambda x: x["phase"] == "monitoring" and not x["busy"], 600):
                print("could not get back to monitoring"); return 2
            time.sleep(75)                                   # past the 60 s detection pause after a reset
        r = requests.post(f"{BASE}/api/incidents/{key}/trigger", timeout=10).json()
        if not r.get("ok"):
            print("trigger refused:", r); return 2
        meta.write_text(str(time.time()))
        t0 = float(meta.read_text())
        for _ in range(150):                                 # when the injection script finished
            done = [o["t"] for o in snap().get("ops", []) if o["text"].startswith(f"[{key}] exited with code")]
            if done:
                (OUT / f"{key}.inject").write_text(str(done[-1] - t0))
                break
            time.sleep(2)
    started = float(meta.read_text())
    inj = OUT / f"{key}.inject"
    INJECT["done"] = round(float(inj.read_text()), 1) if inj.exists() else None
    stop = TERMINAL if resume else TERMINAL | HUMAN
    s = wait_phase(lambda x: x["phase"] in stop and not (resume and x["phase"] in HUMAN), 1500, every=4)
    if s is None:
        s = snap()
        print(f"TIMEOUT waiting; phase={s['phase']}")
    time.sleep(3)
    s = snap()
    r = readings(key, events(), s, started)
    (OUT / f"{key}.json").write_text(json.dumps(r, indent=2, default=str))
    if s["phase"] in HUMAN:
        print("NEEDS HUMAN IN SLACK:", s["phase"])
    print(summary(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
