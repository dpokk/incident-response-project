# Incident Response Demo

A presentation console over the post-Iteration-7 incident investigator (copied from `dpokk/majorproject` @ `0bfa218`).
It adds two LLM agents with read-only tools: the **Investigator Agent**, which investigates, and the **Reviewer
Agent**, which checks the Investigator's conclusion before any fix. Everything else stays as built: deterministic
detection, the rule engine, human approval, typed execution and verification.

```
Kubernetes (real injection) → Detector → rule engine (evidence collection + diagnosis)
  → hybrid routing (deterministic):
       known pattern  (rule confidence ≥ 75%)        → rules lead, the agent VERIFIES (≤ 5 tool calls)
       unfamiliar     (undetermined or < 75%)        → the Investigator Agent LEADS (≤ 14 tool calls)
  → report (findings · cited evidence · root cause · suggested fix or manual steps)
  → Reviewer Agent: agree / challenge / reject, with cited evidence (≤ 4 tool calls; can only block)
  → web console + Slack
  → engineer in Slack: Approve, then Execute → policy → live recheck → dry run → ONE typed change → validation
```

## The hybrid, and how to show it

| Incident | Rule engine | Route | What the agent adds |
|---|---|---|---|
| Wrong database host | High confidence | rules lead, agent verifies | a quick cross-check and the fix |
| Database down | High confidence | rules lead, agent verifies | a quick cross-check and the replica count |
| Memory exhaustion | High confidence | rules lead, agent verifies | the concrete memory limit to propose |
| **Stale database credentials** | Medium (60%), and blames the wrong setting (`DATABASE_URL`) | **the agent leads** | **finds the real cause** (`PGPASSWORD` was switched to `secret/postgres-credentials-rotated`, from configuration history) and gives manual steps, because no typed action fits |

Show one known incident first, then **Stale database credentials**: that's where the agent finds what the rules
can't. Measured live, verifying took 4 tool calls, about 37 s and 21k tokens; leading took 10 tool calls,
about 180 s and 89k tokens.

Safeguards in every route:
- The agent's tools are read-only.
- Every finding's citations are checked against what the agent actually collected.
- The executable action always comes from the deterministic plan, and only a human in Slack can approve and
  execute it.
- If the model fails, the rule engine's findings stand.

## The Reviewer Agent (a second opinion that can only block)

An LLM agent in `demo/agent/reviewer.py`. After the Investigator Agent reports, it checks that report with the same
read-only tools, on the same evidence (it can cite the Investigator's E- and F-ids, and adds its own). It looks for
evidence against the conclusion: a different cause, a fix that would not help, or an unjustified fix value. It runs
the Investigator's loop with its own prompt and final tool (`submit_review`), within 4 tool calls and 60 s per model.

| Verdict | Effect |
|---|---|
| **agree** | nothing changes |
| **challenge** | the Remediation Agent may not act alone (gate check `reviewer_agrees`); an engineer decides in Slack |
| **reject** | the same, and the Investigator's own fix and plan are dropped: the rule engine's plan stands |
| objection without valid evidence | shown as "unsupported" and ignored |
| model unavailable, or no Investigator report | no review: the flow is exactly as without the Reviewer |

It never proposes a fix, never raises confidence, never approves and never changes anything. Its verdict is a
reply under the Investigation message in Slack and its own section on the console.

**Model:** `DEMO_REVIEWER_MODEL` (default `nvidia/nemotron-3.5-super-vl-120b-a12b`). If that model is unavailable
(timeout, 404, error) the review runs once more with the Investigator's model (`DEMO_AGENT_MODEL`). Checked on
2026-11-10 against the fake cluster: it agreed with a correct diagnosis (3 tool calls, 14.5 s) and rejected a planted
wrong one, with both objections citing real evidence (4 tool calls, 18.2 s). Covered by `tests/test_demo_reviewer.py`.

## The Remediation Agent (graduated autonomy)

A deterministic agent (no LLM), in `demo/remediation_agent.py`. When its policy gate passes, it replaces the
engineer's two Slack clicks. It drives the **same** executor an engineer would.

**Gate** (`demo/remediation_agent_policy.json`). Every check must pass:
- automatic remediation is switched on (the header switch);
- known pattern (the rules led);
- rule confidence ≥ 90%;
- the Investigator Agent agrees;
- every finding is evidence-checked;
- the Reviewer Agent does not challenge or reject (with valid evidence);
- the action is `scale_workload` or `adjust_resource_limit`;
- a validated value exists;
- the component isn't locked.

**Attempts** follow a policy ladder:
- memory: the agent's value, then ×1.5 (rounded to 64Mi, capped at 1Gi), at most 3 attempts;
- replicas: one attempt (more replicas is not a fix, and is unsafe for a database).

The next value is tried only after verification says NOT_RESOLVED or INCONCLUSIVE. A refused or failed attempt stops.
An uncertain state is never retried or reverted.

**Always revert:** if nothing was verified as resolved, the value from before the first attempt is restored (through
the executor). Then Slack shows what was tried and an **Acknowledge** button. The component stays locked against
automation until an engineer acknowledges (circuit breaker).

**Stop** (Slack, while it runs): no further attempt and no revert; the engineer who pressed it owns the incident.

**Console:** the "On the incident" strip shows who is working at each moment and every hand-off:
- Detector;
- Rule engine;
- Investigator Agent (working → offline when it submits its report);
- Reviewer Agent (working → offline when it submits its verdict);
- Remediation Agent;
- Engineer.

Measured live:

The gate threshold is 85%.

| Incident | Rules | Result |
|---|---|---|
| Database down | 97% | auto-scaled 0 → 1, RESOLVED, no human click |
| Memory limit set too low | 97% | auto-raised 32Mi → 64Mi, RESOLVED on attempt 1, no human click |
| Memory exhaustion (spike) | 70–85%, varies with timing | Slack approval; the spike also ends by itself |
| Application crash | 90% | agent verified the exact line; no typed action fits, so manual steps |
| Broken release (missing image) | 90% (63% once, agent led) | plans `rollback_release` app:0.3 → app:0.2 from recorded history; human Approve + Execute |
| Backend scaled to zero | no pattern, agent led | agent found it (scale to 2); not executable, because the rules' plan has no action |
| Sustained overload | 49% | the gate refuses; a human decides |

The retry, revert, Stop and lock paths are covered by `tests/test_demo_remediation_agent.py`.

## Run it

Prerequisites:
- **Docker Desktop is running.** The `Start services` button starts the minikube cluster if needed.
- **`.env`** is present (not in git). It needs `SLACK_*` for Slack and `NVIDIA_API_KEY` for the agent.

```powershell
pip install -r requirements.txt fastapi uvicorn
python -m demo            # opens http://127.0.0.1:8800
```

## Presenting

1. **Start services.** This connects to the cluster and starts detection, live status, live logs and the Slack
   listener.
2. **Show the healthy system:**
   - components and pods;
   - synthetic user requests;
   - traffic and the 5xx ratio (from Prometheus);
   - the normal logs.
3. **Trigger one incident** in the Incident lab. The real script in `scripts/inject/` runs.
4. **Watch it fail.** The status turns red and the logs change. Detection opens the incident after collecting
   symptoms for 20 s.
5. **The investigation runs:**
   - first, the rule engine's evidence collection (its decisions and capability calls);
   - then the **Investigator Agent**'s tool calls, each with its purpose and result;
   - then the **Reviewer Agent**'s tool calls and its verdict.
6. **Read the report.** It shows the root cause, the evidence-checked findings, the suggested fix, and whether the
   agent agrees with the rule engine. Slack gets the same, as headed messages: **Incident detected** (its status
   line kept current), **Investigation** (root cause, key evidence, the agent's verdict; details in a reply),
   **Remediation** (the fix and its buttons; approvals and execution progress as replies), and a separate
   **final status** message once the incident concludes.
7. **Approve in Slack, then Execute in Slack.** The console takes no decisions. It mirrors them from the shared
   review and execution records, with a link to the Slack thread. Execute runs the deterministic remediation and then
   validation, shown live in both places.
8. **🟢 RESOLVED** appears in the page and in Slack.
9. **Reset to healthy** before the next incident. It also undoes the credentials incident
   (`scripts/restore-credentials.ps1`). For that incident there is no automatic fix: the console and Slack show the
   agent's manual steps.

## Short blips, timing and reconnecting

- **Short blips do not open incidents.** Error-log bursts and the 5xx ratio lag the system: one stall of a few
  seconds stays in the 15 s rate window for several polls. On their own (the synthetic probe passing) they must hold
  for `LAGGING_SIGNAL_SUSTAIN_S` (30 s). With the probe failing in the same poll, two polls are enough, so every
  injected incident is detected as fast as before. Each error log line is counted in one poll only.
- **Both agents get measured timing facts:** the incident window, and whether the symptoms are still present now
  (from the detector's latest sample). Tool results say when each restart, change, event and rollout happened
  relative to detection, and whether it is inside the incident window, so history is not read as the cause. The
  console shows "Symptoms cleared before the investigation started" when that is the case.
- **After a cluster restart** (its API endpoint can move), the monitor and the log reader read the kubeconfig
  again, and the Prometheus client restarts its port-forward, each at most every 15 s. When the cluster answers
  again, detection starts afresh after 90 s, so the restart of every container is not reported as an incident.
- **If the Investigator's model stops answering** (NVIDIA timeouts), the same investigation, with every tool call so
  far, continues with the Reviewer's model. The console and Slack show which model finished it.
- Checked live on 2026-11-10: freezing both backend pods for 3 s (84 error lines, 5xx at 8.6%) opened no incident;
  Database down was detected 6 s after the trigger and fixed automatically; a killed port-forward was restarted
  with no gap in the traffic readings; `minikube stop` and `start` under the running demo (API port 65174 to 60147)
  recovered without a restart of the demo and without a false incident.

## What is real

Nothing in the page is simulated:
- **Status** comes from the detector's own sample (the Kubernetes API), plus Prometheus for traffic.
- **Logs** are the containers' own output.
- **Agent steps** are the model's actual tool calls and their results.
- **Remediation and validation** come from the Iteration 7 execution service and its audit record.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DEMO_AGENT_MODEL` | `nvidia/nemotron-3-super-120b-a12b` | Model on the NVIDIA NIM endpoint (native tool calling) |
| `DEMO_REVIEWER_MODEL` | `nvidia/nemotron-3.5-super-vl-120b-a12b` | The Reviewer Agent's model; falls back to `DEMO_AGENT_MODEL`. Empty: use `DEMO_AGENT_MODEL` only |
| `DEMO_REVIEWER_TIMEOUT_S` | `30` | Per-request timeout for the Reviewer's model, so a slow endpoint falls back quickly |
| `INVESTIGATE_DELAY_S` | `20` | Symptom-collection time before investigating |
| `LAGGING_SIGNAL_SUSTAIN_S` | `30` | How long error-log bursts or the 5xx ratio must hold on their own to open an incident |
| `EXECUTION_POLICY_PATH` | `demo/execution_policy.json` | The same limits as the main project, with a 60 s observation window |
| `DEMO_PORT` | `8800` | Web port |

## Layout

| Path | Role |
|---|---|
| `demo/server.py` | FastAPI routes and the Server-Sent Events stream |
| `demo/engine.py` | Wires the existing components together and publishes each real step |
| `demo/live.py` | Component status and log tailing |
| `demo/agent/` | `llm.py` (model adapter), `tools.py` (read-only tools over capabilities), `investigator.py` (the loop and report validation), `reviewer.py` (the Reviewer Agent and review validation) |
| `demo/slack_ai.py` | The Investigation (rules + agent), the Reviewer's verdict, the Remediation note, the Remediation Agent's replies and the final status |
| `demo/web/index.html` | The console |

The `investigator/` package matches the main project except for three changes: the `rollback_release` action (planner rule, recheck, `set_image` actuator, policy entry, Slack labels), the Slack layout (`slack_view.py`, `slack.py`: headed channel messages instead of one thread, shorter content), and detection and reconnecting (`detector.py`: short blips; `kube.py`, `prom.py`, `providers.py`: reconnect after a cluster restart).
