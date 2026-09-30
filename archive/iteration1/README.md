# Kubernetes Incident Investigation â€” Prototype 1

A local Kubernetes environment with a deliberately fragile service, a traffic spike to break it, and an
investigation service that detects the incident, collects evidence, reconstructs the timeline, identifies the
root cause and impact, and posts a report to Slack.

```
Incident â†’ Context â†’ Evidence â†’ Correlation â†’ Timeline â†’ Root cause â†’ Impact â†’ Report â†’ Slack
```

## Architecture

```
 loadtest ns                shop ns (the "production" system)                monitoring ns
â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”   HTTP    â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”   HTTP   â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”            â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
â”‚ loadgen  â”‚ â”€â”€â”€â”€â”€â”€â”€â”€â–º â”‚ frontend â”‚ â”€â”€â”€â”€â”€â”€â”€â–º â”‚ backend  (x2)  â”‚ â—„â”€ scrape â”€â”‚ Prometheus â”‚
â”‚ 100â†’800  â”‚           â”‚ gateway  â”‚          â”‚ 500m CPU/192Mi â”‚            â”‚ app+cAdvisorâ”‚
â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜           â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜          â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜            â””â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”˜
                                                                                 â”‚
                         host: python -m investigator watch                      â”‚
   â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”´â”€â”€â”€â”
   â”‚ Detector (thresholds) â†’ PodJournal (pod watch) â†’ Collector (K8s API, events,   â”‚
   â”‚ logs incl. --previous, PromQL range queries) â†’ Analysis (timeline, hypotheses, â”‚
   â”‚ impact, evidence IDs) â†’ Claude (narrative, JSON schema) â†’ Report â†’ Slack        â”‚
   â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
```

### Why the backend fails (the simulated failure mode)

Each backend request allocates a 256 KB working buffer and needs ~3 ms of CPU. With a 500m CPU limit a pod
can process roughly 150 req/s. At the 100 req/s baseline (50 per pod) it runs at ~35% of its CPU limit.
When traffic ramps to 800 req/s, requests arrive faster than the CPU-throttled workers can finish them. They
queue, each still holding its buffer, so memory grows with the backlog until the container hits its 192 Mi
limit and is **OOMKilled**. Readiness probes fail as the backlog grows, the Service loses ready endpoints,
restarted pods are immediately flooded again (CrashLoopBackOff), and the frontend returns 502/503/504. When
the spike ends, the pods come back and errors return to baseline.

The investigator is not told any of this. It works it out from metrics, events, pod status and logs.

## Components

| Path | What |
|---|---|
| `app/backend.py` | Orders API: `/api/orders`, `/healthz`, `/readyz`, `/metrics`, JSON logs incl. periodic `stats` lines |
| `app/frontend.py` | Gateway that proxies to the backend; its metrics are the user-facing error rate |
| `app/loadgen.py`, `app/loadgenctl.py` | Open-loop traffic generator with a control API (`spike`, `stop`, `base`) |
| `k8s/` | Namespaces, Deployments, Services, ConfigMap, resource requests/limits, probes, Prometheus + RBAC |
| `investigator/detector.py` | Detects the incident: 5xx rate, pod restarts, CPU or memory vs limit |
| `investigator/kube.py` | K8s API access and the **pod journal**, a watch that records every OOMKill, restart and readiness change (K8s itself only keeps the last one) |
| `investigator/collector.py` | Gathers evidence for the incident window: deployments, ReplicaSets, ConfigMaps, services/endpoints, pods, events, node events, logs (current and previous container), and 14 PromQL range queries |
| `investigator/analysis.py` | Deterministic correlation. Builds baselines, threshold crossings, terminations, ready-endpoint reconstruction, recovery, change detection, **scored hypotheses** (traffic exhaustion, bad change, memory leak, dependency failure, node failure), confidence and impact. Every fact carries evidence IDs. |
| `investigator/llm.py` | Sends the structured analysis (not raw dumps) to the LLM with a JSON schema. The default is `deepseek-ai/deepseek-v4.1-flash` on NVIDIA's free OpenAI-compatible API (build.nvidia.com); Claude is optional via `LLM_PROVIDER=anthropic`. Output is schema-checked and cited evidence IDs are validated. If the LLM isn't available, it falls back to a rule-based narrative. |
| `investigator/report.py`, `slack.py` | Report as Markdown/JSON plus the raw evidence bundle, and Slack Block Kit via bot token or webhook |

## Prerequisites

- Docker Desktop, minikube, kubectl (all already on this machine)
- Python 3.10+ and `pip install -r requirements.txt`
- LLM key: a free API key from build.nvidia.com in `NVIDIA_API_KEY` (verify with `python -m investigator llm-test`)
- Optional: Slack, which needs either:
  - **Bot token:** create an app at api.slack.com/apps, add the `chat:write` bot scope, install it to the
    workspace, copy the `xoxb-â€¦` token to `SLACK_BOT_TOKEN`, put the channel ID in `SLACK_CHANNEL`, and run
    `/invite @yourbot` in that channel.
  - **Webhook:** turn on Incoming Webhooks and put the URL in `SLACK_WEBHOOK_URL`.

```powershell
copy .env.example .env      # then fill in tokens
pip install -r requirements.txt
```

## Running the demo

```powershell
# 1. Build the image in minikube and deploy everything (~2-3 min the first time)
.\scripts\setup.ps1

# 2. Verify connectivity (K8s, Prometheus via auto port-forward, Slack, LLM)
python -m investigator check

# 3. Terminal A: start the investigator. It prints one live health line every 5s ("normal metrics").
python -m investigator watch

# 4. Terminal B: after ~2 min of healthy baseline traffic, trigger the spike
.\scripts\spike.ps1                   # 100 -> 800 req/s, 10s ramp, 90s hold
kubectl get pods -n shop -w           # watch OOMKilled / CrashLoopBackOff happen
```

Terminal A then shows the detection. When the system has been stable for 45 s, the investigator runs the
five steps and posts the report to Slack. Reports are also saved to `reports/INC-*.md|json|evidence.json`.

Other commands:

```powershell
python -m investigator status                    # live health only
python -m investigator investigate --since 15m   # on-demand investigation of a past window
python -m investigator replay reports\INC-...evidence.json   # re-run analysis on saved evidence
python -m investigator post reports\INC-....json --note "corrected re-analysis"   # (re)post a saved report
python -m investigator llm-test                  # check the NVIDIA key / model only
.\scripts\reset.ps1                              # stop spike, restart backend, clean slate
```

## Tuning

If your machine doesn't produce OOMKills, or does so too early, adjust `backend-config` in
`k8s/10-shop.yaml` (`CPU_WORK_MS`, `REQUEST_BUFFER_KB`), the backend limits, or the spike size
(`.\scripts\spike.ps1 -PeakRps 600`). Re-apply with `kubectl apply -f k8s` and restart the backend.

