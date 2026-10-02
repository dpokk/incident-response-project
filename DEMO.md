# Incident Response Demo

A presentation console over the post-Iteration-7 incident investigator (copied from `dpokk/majorproject` @ `0bfa218`).
It adds an **LLM Investigator Agent** that investigates with read-only tools. Everything else stays as built:
deterministic detection, the rule engine, human approval, typed execution and verification.

```
Kubernetes (real injection) → Detector → rule engine (evidence collection + diagnosis)
  → hybrid routing (deterministic):
       known pattern  (rule confidence ≥ 75%)        → rules lead, the agent VERIFIES (≤ 5 tool calls)
       unfamiliar     (undetermined or < 75%)        → the Investigator Agent LEADS (≤ 14 tool calls)
  → report (findings · cited evidence · root cause · suggested fix or manual steps) → web console + Slack
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

## The Remediation Agent (graduated autonomy)

A deterministic agent (no LLM), in `demo/remediation_agent.py`. When its policy gate passes, it replaces the
engineer's two Slack clicks. It drives the **same** executor an engineer would.

**Gate** (`demo/remediation_agent_policy.json`). Every check must pass:
- automatic remediation is switched on (the header switch);
- known pattern (the rules led);
- rule confidence ≥ 90%;
- the Investigator Agent agrees;
- every finding is evidence-checked;
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
   - then the **Investigator Agent**'s tool calls, each with its purpose and result.
6. **Read the report.** It shows the root cause, the evidence-checked findings, the suggested fix, and whether the
   agent agrees with the rule engine. The same report is posted to the Slack thread.
7. **Approve in Slack, then Execute in Slack.** The console takes no decisions. It mirrors them from the shared
   review and execution records, with a link to the Slack thread. Execute runs the deterministic remediation and then
   validation, shown live in both places.
8. **🟢 RESOLVED** appears in the page and in Slack.
9. **Reset to healthy** before the next incident. It also undoes the credentials incident
   (`scripts/restore-credentials.ps1`). For that incident there is no automatic fix: the console and Slack show the
   agent's manual steps.

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
| `INVESTIGATE_DELAY_S` | `20` | Symptom-collection time before investigating |
| `EXECUTION_POLICY_PATH` | `demo/execution_policy.json` | The same limits as the main project, with a 60 s observation window |
| `DEMO_PORT` | `8800` | Web port |

## Layout

| Path | Role |
|---|---|
| `demo/server.py` | FastAPI routes and the Server-Sent Events stream |
| `demo/engine.py` | Wires the existing components together and publishes each real step |
| `demo/live.py` | Component status and log tailing |
| `demo/agent/` | `llm.py` (model adapter), `tools.py` (read-only tools over capabilities), `investigator.py` (the loop and report validation) |
| `demo/slack_ai.py` | The agent's Slack messages |
| `demo/web/index.html` | The console |

The `investigator/` package is unchanged from the main project.
