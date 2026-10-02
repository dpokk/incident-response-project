# Incident Response Demo

A presentation console over the post-Iteration-7 incident investigator (copied from `dpokk/majorproject` @ `0bfa218`).
It adds an **LLM Investigator Agent** that investigates with read-only tools. Everything else stays as built:
deterministic detection, the rule engine, human approval, typed execution and verification.

```
Kubernetes (real injection) → Detector → rule-based evidence collection → Investigator Agent (LLM, read-only tools)
  → report (findings · evidence · root cause · suggested fix) → web console + Slack
  → human APPROVE / REJECT → policy → live recheck → dry run → ONE typed change → validation → RESOLVED
```

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
9. **Reset to healthy** before the next incident.

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
