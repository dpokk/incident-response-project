# Kubernetes Incident Investigation: Iteration 2

A generalized, evidence-driven incident investigator. Failures are injected into a running Kubernetes
application. The investigator is **not told what failed**. It collects facts from the live system, works
out the affected component and failure category, and writes a structured incident report. It does no
remediation.

```
Detection  ->  Evidence collection  ->  Diagnosis  ->  Incident report      [future: Remediation -> Verification]
```

Iteration 1 (single OOM scenario, LLM narrative) is archived in `archive/iteration1/`.

## Architecture

```
 loadtest ns             shop ns (system under investigation)                    host
┌──────────┐  HTTP  ┌──────────┐  HTTP  ┌──────────────┐  SQL  ┌────────────┐    ┌──────────────────────┐
│ loadgen  │ ─────► │ frontend │ ─────► │ backend (x2) │ ────► │ PostgreSQL │    │ python -m investigator│
│ 100 rps  │        │ gateway  │        │ orders API + │       │ (Service   │    │  detector            │
└──────────┘        └──────────┘        │ invoice job  │       │  postgres) │    │  toolset (read-only) │
                                        └──────────────┘       └────────────┘    │  collect -> diagnose │
                                                                                 │  -> report (+Slack)  │
                                                                                 └──────────────────────┘
```

The backend stores every order in PostgreSQL. A background invoice job computes unit prices, and it
fails fast by design: an unexpected exception terminates the process.

## Failure scenarios (injected by scripts that tell the investigator nothing)

| Demo | Injection script | What actually breaks | Expected diagnosis |
|---|---|---|---|
| A | `scripts\inject\oom.ps1` | 100 → 800 req/s; request backlog grows until the 192Mi limit is hit | Memory exhaustion (OOMKilled) in `backend`; traffic increase as a contributing factor |
| B | `scripts\inject\db-misconfig.ps1` | `DATABASE_URL` host changed to `postgres-wrong` and rolled out | Dependency configuration/connectivity error: host doesn't exist, while healthy `postgres` does |
| C | `scripts\inject\db-down.ps1` | PostgreSQL Deployment scaled to 0 | Dependency unavailable: config is valid, Service has 0 endpoints, TCP refused |
| D | `scripts\inject\app-crash.ps1` | One user order with `quantity=0` → invoice job raises `DivisionByZero` → exit 1 → CrashLoopBackOff | Application crash (not OOM), with exception type and code location |

`scripts\restore.ps1` recovers manually from any of them.

## How the investigator works

**Detection** (`detector.py`) reports only symptoms, never causes:
- container restarts
- containers stuck waiting (CrashLoopBackOff, image pull, config errors)
- pods NotReady for longer than a grace period
- unschedulable pods
- a synthetic user request to the frontend (via the API server's service proxy) failing
- a burst of error-level log lines
- optionally, the Prometheus 5xx ratio

**Evidence collection** (`collect.py`) runs the same process for every incident, over every workload in
the namespace, using read-only tools from `tools.py` (`get_pods`, `get_pod_status`, `get_container_status`,
`get_pod_events`, `get_logs`, `get_services`, `get_endpoints`, `get_configuration`, `check_dependency`, …):

1. Workloads, replica counts, pod phase/readiness, container state, and termination history (reason,
   exit code, runtime). The pod journal keeps terminations that Kubernetes itself forgets.
2. Kubernetes events (pods, ReplicaSets, Deployments, nodes).
3. Logs from current and previous container instances. `logparse.py` extracts generic signatures:
   DNS failure, connection refused/timeout/reset, auth failure, memory pressure, upstream errors,
   Python tracebacks with the crash site.
4. Services and ready endpoints.
5. Configuration. Effective env from ConfigMaps and Secrets; secret values are never reported.
6. Dependencies (`dependencies.py`), discovered from the configuration (URLs, `*_HOST`/`*_PORT`). For each
   one it checks:
   - whether the Service exists and exposes the port;
   - its endpoints and the backing workload;
   - similar services, if the name doesn't exist;
   - DNS and TCP from inside the consumer pod, using a fixed read-only probe.
7. Change history: rollouts and ConfigMap edits.
8. Optional metrics: traffic change, error ratio, memory versus limit.

Every observation is stored as a **fact** (`evidence.py`) with an ID, source, subject and timestamp.
Facts carry no interpretation.

**Diagnosis** (`diagnosis.py`) checks each workload against a general failure taxonomy:
- memory exhaustion
- application crash
- dependency misconfiguration
- dependency unavailable
- image pull failure
- container configuration error
- unschedulable
- health-check failure

Each check scores the facts that support it and records the facts that contradict it. Findings are
followed along the dependency graph built from configuration: if a consumer's errors come from a
dependency that is failing for its own reason, the dependency is the root cause and the consumer is
marked as *impacted*. There is no scenario flag anywhere. `tests/test_scenarios.py` asserts that.

**Report** (`report.py`) uses the same structure for every category:
- Incident ID
- affected component
- failure category
- dependencies involved
- observed symptoms (facts)
- evidence (facts with sources)
- diagnosis (interpretation, each statement citing fact IDs)
- likely root cause
- confidence
- alternatives considered, with why they were rejected
- timeline
- investigation trace

It's saved as `.txt`, `.md`, `.json` and `.evidence.json`, and optionally posted to Slack.

## Running it

Run everything from the project folder with Docker Desktop running.

```powershell
pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1     # start cluster, build image, deploy (incl. PostgreSQL)
python -m investigator check                                   # K8s, entry probe, metrics, Slack
python -m pytest tests -q                                      # offline tests: 4 scenarios + healthy, no hints
```

Demo (two terminals):

```powershell
# Terminal A: detection -> investigation -> report, automatically
python -m investigator watch            # add --no-slack to keep reports local

# Terminal B: inject ONE failure, wait for the report in terminal A, then restore
powershell -ExecutionPolicy Bypass -File scripts\inject\db-misconfig.ps1
powershell -ExecutionPolicy Bypass -File scripts\restore.ps1
```

Wait about a minute after a restore before injecting the next failure, so detection starts from a
healthy baseline.

Other commands:

```powershell
python -m investigator status                   # live health line every 5 s
python -m investigator investigate --since 10m  # on-demand investigation of the current state
python -m investigator replay reports\INC-....evidence.json   # re-diagnose saved evidence offline
python -m investigator post reports\INC-....json              # post a saved report to Slack
minikube stop -p incident-demo                  # stop the cluster when done
```

## Explicitly out of scope (Iteration 2)

LLM/AI reasoning, automated remediation, new observability stacks (Prometheus from Iteration 1 is only
optional enrichment), and handling every possible Kubernetes error.
