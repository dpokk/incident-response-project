# Incident Investigation Prototype: Iteration 4 in progress (historical evidence)

An evidence-driven incident investigator, proven first on a local Kubernetes application. Failures are
injected into the running system. The investigator is **not told what failed**. It decides which
evidence is relevant and gathers it through provider-independent **capabilities**, then works out the
affected component and failure category and writes a structured incident report. It does no
remediation.

```
Detection -> Incident context -> Investigation planner <-> Capabilities (Kubernetes / Prometheus adapters)
          -> Diagnosis -> Incident report                     [future: Remediation -> Verification]
```

Project direction, roadmap and architecture principles live in `CLAUDE.md` and `docs/`. Iteration 1 is
archived in `archive/iteration1/`.

**Status:**
- **Iteration 3 is architecturally complete.** Done:
  - step 1: capability interface + adapters;
  - step 2: incident context + planner;
  - step 3: detection on capabilities;
  - step 4: provider-neutral fact vocabulary.
- **Iteration 4 (historical evidence and incident reconstruction) is in progress.** Done so far:
  - **Evidence retention.** A recorder keeps what Kubernetes forgets, so a crash can be diagnosed after the
    crashed pods are gone.
  - **Timeline reconstruction.** Phases, ordering and uncertainty, inferred from the facts.
  - **Metrics correlation.** Threshold crossings with bounded times; traffic is linked to an OOM only by
    order, component and call path.

  - **Impact assessment.** Affected and gone instances, dependency state, impacted callers, users (failed
    requests estimated only when metrics allow), duration, propagation and blast radius.

  Next: the live OOM post-mortem validation (`docs/ROADMAP.md`).
- **Kubernetes is the only resource provider implemented so far.** Provider agnosticism is not claimed
  until a second provider exercises the same interface (Iteration 8 in `docs/ROADMAP.md`).

## Architecture

```
 loadtest ns             shop ns (system under investigation)                    host
┌──────────┐  HTTP  ┌──────────┐  HTTP  ┌──────────────┐  SQL  ┌────────────┐    ┌──────────────────────┐
│ loadgen  │ ─────► │ frontend │ ─────► │ backend (x2) │ ────► │ PostgreSQL │    │ python -m investigator│
│ 100 rps  │        │ gateway  │        │ orders API + │       │ (Service   │    │  detector            │
└──────────┘        └──────────┘        │ invoice job  │       │  postgres) │    │  context -> planner  │
                                        └──────────────┘       └────────────┘    │  capabilities ──────►│ adapters
                                                                                 │  diagnose -> report  │
                                                                                 └──────────────────────┘
```

Investigator layout:

| Layer | Files | Provider-specific? |
|---|---|---|
| Detection | `detector.py` | no |
| Incident context, planner | `context.py`, `planner.py` | no |
| Evidence recording | `collect.py`, `dependencies.py`, `metrics.py`, `logparse.py`, `evidence.py` | no |
| Capability interface | `capabilities/base.py`, `capabilities/__init__.py`, `capabilities/references.py` | no |
| Adapters | `capabilities/kubernetes.py` (+ `kube.py`), `capabilities/prometheus.py` (+ `prom.py`) | yes, by design |
| Evidence history | `capabilities/kubernetes_recorder.py` (records), `history_store.py` (SQLite store) | recorder yes; store no |
| Composition root | `providers.py` (chooses the adapters) | the one place that names them |
| Diagnosis, report, CLI | `diagnosis.py`, `report.py`, `slack.py`, `pipeline.py`, `__main__.py` | no |
| Compatibility | `legacy.py` (reads evidence saved in the old Kubernetes vocabulary) | yes, replay only |

`tests/test_architecture.py` enforces two rules:
- **No provider imports.** It fails if any provider-independent module imports provider code.
- **No provider vocabulary.** It fails if reasoning code uses Kubernetes vocabulary (`OOMKilled`,
  `CrashLoopBackOff`, exit code 137, …).

Diagnosis reasons over neutral classifications that the adapter maps from provider values:
- **termination cause:** `memory_limit`, `error_exit`, `killed`, `completed`;
- **event category:** `restart_backoff`, `health_check_failed`, `scheduling_failed`, …;
- **waiting cause.**

Provider words such as "OOMKilled" still appear in reports, but only as values taken from facts.

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

**Capabilities** (`capabilities/`) are the only way the investigation obtains evidence. The interface is
provider-independent:

| Capability | Kubernetes adapter implements it with |
|---|---|
| `list_components`, `get_resource_state` | Deployments/StatefulSets, Pods, container state, the pod journal (terminations Kubernetes forgets) |
| `get_events` | Namespace events attributed to components; node events as infrastructure events |
| `get_logs` | Current and previous container logs |
| `get_configuration`, `get_dependencies` | Effective env from ConfigMaps and Secrets (values never reported); URLs and `*_HOST`/`*_PORT` pairs |
| `list_services`, `get_service_health` | Services, endpoints, backing workloads, similar services when a name doesn't exist |
| `check_connectivity` | A fixed, read-only DNS + TCP probe from inside a running consumer pod |
| `probe_request` | A synthetic user request through the API server's service proxy |
| `get_deployment_history` | ReplicaSet revisions |
| `get_metrics` | Separate `PrometheusMetrics` adapter: request rate, error ratio, memory (optional) |
| `get_log_history` | Retained logs of runs Kubernetes no longer serves (older runs, deleted pods) |
| `get_configuration_history` | Recorded ConfigMap / Secret (fingerprint only) / workload definition changes, with exact or bounded times |
| `get_evidence_coverage` | When the recorder was running; gaps are stated in the report |

**Evidence history.** Kubernetes keeps only a container's current and previous run, forgets a deleted pod
at once, and expires events after about an hour. While `watch` (or `record`) runs, a recorder keeps all of
these in `state/history.db` for 24 hours. The adapter serves the retained evidence marked as `retained`.
The report lists what the history covered and what it did not.

Records use a neutral vocabulary: a *component* has *instances* that run *processes*. Every call is
traced, including which provider served it, and cached for the investigation.

**Incident context and planner** (`context.py`, `planner.py`). The context holds only the detection
signals, the time window and the components those signals name. The planner starts there and follows
the evidence:

- a process restarted → read its previous instance's logs;
- errors about a dependency, or an unhealthy dependency → test connectivity from inside the consumer, and
  examine the component that serves it;
- a suspect with no failure of its own → follow its dependencies downstream;
- signs of resource exhaustion → query metrics;
- nothing failing found → widen to every component.

Every decision is recorded in the investigation trace with its reason. The fixed Iteration 2 procedure
is kept as `INVESTIGATION_STRATEGY=exhaustive`, for comparison; the default is `planned`.

**Evidence** (`collect.py`, `dependencies.py`, `metrics.py`, `logparse.py`). Capability results become
**facts** (`evidence.py`) with an ID, source (including the provider), subject and timestamp. Facts carry
no interpretation. `logparse.py` extracts generic log signatures: DNS failure, connection
refused/timeout/reset, auth failure, memory pressure, upstream errors, and Python tracebacks with the
crash site.

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
- impact (evidence-backed, separate from the root cause), with what could not be quantified
- confidence
- alternatives considered, with why they were rejected
- reconstructed timeline (inferred): phases from onset to recovery, what changed first, when symptoms began,
  the earliest recorded failure, what preceded it, ordering (never cause) and stated uncertainty
- observed timeline entries, and evidence coverage and limitations
- investigation trace

It's saved as `.txt`, `.md`, `.json` and `.evidence.json`, and optionally posted to Slack.

## Running it

Run everything from the project folder with Docker Desktop running.

```powershell
pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1     # start cluster, build image, deploy (incl. PostgreSQL)
python -m investigator check                                   # K8s, entry probe, metrics, Slack
python -m pytest tests -q                                      # offline tests: scenarios, planner, architecture
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
python -m investigator record                   # only record evidence history (watch also records)
python -m investigator investigate --since 10m  # on-demand investigation, using retained history
python -m investigator replay reports\INC-....evidence.json   # re-diagnose saved evidence offline
python -m investigator post reports\INC-....json              # post a saved report to Slack
minikube stop -p incident-demo                  # stop the cluster when done
```

## Explicitly out of scope (Iterations 3–4)

The following are later milestones in `docs/ROADMAP.md`:
- an LLM-assisted planner;
- a second provider;
- automated remediation;
- new observability stacks (Prometheus from Iteration 1 is only optional enrichment);
- SaaS, multi-tenancy and the customer connector.
