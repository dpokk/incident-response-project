# ARCHITECTURE.md

## Architectural Purpose

This document records the architecture principles that should guide evolution from the current Kubernetes investigation prototype toward a provider-agnostic incident investigation and remediation service.

## 1. Current Architecture

The current system is primarily a Kubernetes-based, evidence-driven investigator.

Conceptually:

```text
Incident
   ↓
Detection
   ↓
Evidence Collection
   ↓
Dependency Analysis
   ↓
Diagnosis
   ↓
Timeline
   ↓
Structured Report
```

Evidence currently comes from Kubernetes and the application environment.

The current system is deterministic and evidence-driven. It is not yet an autonomous AI investigator.

## 2. Core Architectural Boundary

Separate:

### Provider-independent investigation logic

Responsible for:
- Incident context
- Investigation strategy
- Evidence interpretation
- Correlation
- Timeline reconstruction
- Root-cause reasoning
- Impact/blast-radius reasoning
- Report generation

### Provider-specific access

Responsible for:
- How logs are retrieved
- How metrics are queried
- How service/resource state is retrieved
- How events are retrieved
- How configuration/deployment state is retrieved
- Eventually how approved remediation actions are executed

Provider-specific code should be isolated behind interfaces/adapters where practical.

## 3. Capability-Based Model

The preferred abstraction is a capability interface.

Examples:

```text
get_logs(target, time_range)
get_metrics(target, time_range)
get_service_health(target)
get_resource_state(target)
get_events(target, time_range)
get_dependencies(target)
get_configuration(target)
get_deployment_history(target, time_range)
```

The investigation engine should ask for capabilities rather than directly embedding provider-specific commands.

Kubernetes is the first adapter.

Future examples:
- AWS adapter
- GCP adapter
- Azure adapter
- Datadog adapter
- Prometheus adapter
- Loki/logging adapter

Do not add an adapter solely for appearance. It should expose useful evidence/capabilities.

## 4. Evidence-First Architecture

Infrastructure state should be represented as evidence before it becomes diagnosis.

Preferred chain:

```text
Raw Source
   ↓
Normalized Evidence
   ↓
Observation / Fact
   ↓
Correlation
   ↓
Diagnosis
   ↓
Root Cause
```

A diagnosis should be traceable to supporting evidence.

Example:

```text
Fact:
PostgreSQL Service has 0 endpoints.

Fact:
Backend logs show connection failures.

Dependency:
Backend depends on PostgreSQL.

Diagnosis:
PostgreSQL is unavailable and is causing backend failures.
```

Avoid allowing an LLM to invent infrastructure state.

## 5. Symptom vs Root Cause

The system must explicitly distinguish:
- Trigger/symptom
- Evidence
- Root cause
- Impacted components
- Alternative explanations

Example:

```text
HTTP 500 increase
    = symptom

PostgreSQL unavailable
    = root cause

Frontend unable to load data
    = impact
```

## 6. Dependency-Aware Investigation

The investigation should understand component relationships.

Example:

```text
Frontend
   ↓
Backend
   ↓
PostgreSQL
```

A failure in PostgreSQL may produce backend errors and frontend errors.

The system should avoid selecting the highest-level visible symptom as the root cause when a downstream dependency explains it.

## 7. Timeline Architecture

Timeline entries should be derived from timestamped evidence where possible.

The timeline should support:
- Incident start
- Important state changes
- Relevant log events
- Metric threshold crossings
- Kubernetes events
- Deployment/configuration changes
- Recovery events

The system should not fabricate exact timestamps.

If timestamps are uncertain, represent uncertainty rather than inventing precision.

**As implemented:**
- **Three times per fact, kept apart.**
  - `t`: event time. How to read it is given by `t_basis`, see below.
  - `collected_at`: when the investigation collected the fact.
  - `observed_at`: when the source saw it; recorded where the source provides it.
- **`origin`.** One of `live`, `retained` or `mixed`; says whether the evidence came from the system now
  or from evidence history.
- **Time basis on every fact.** `t_basis` takes one of five values:
  - `exact`: the source stated the time.
  - `bounded`: known only to lie between `t_earliest` and `t_latest`, e.g. a change seen between two
    recorder observations.
  - `observed`: only when it was seen.
  - `before_window`.
  - `unknown`.
- **Report wording.** Reports print bounded times as "by HH:MM:SS" and observed ones as "seen HH:MM:SS".
- **Pre-window events.** An aggregated event whose first occurrence predates the investigation window
  keeps the source's own `first_seen` and is placed at `observed_at`, its last observation. Reports list
  it as "before window" and never re-date it to the window start.
- **Metric threshold crossings** (CPU/memory saturation, error-rate recovery) are not yet timeline
  entries. See `docs/INCIDENTS.md`, Incident 5.

## 8. AI/LLM Boundary

The eventual system may use an LLM for:
- Investigation planning
- Selecting relevant capabilities
- Interpreting heterogeneous evidence
- Generating explanations
- Generating remediation plans

The LLM should not be the only source of truth for:
- Resource state
- Incident timestamps
- Configuration values
- Kubernetes state
- Whether a remediation succeeded

Those should be grounded in tool evidence.

## 9. Remediation Boundary

Investigation and remediation should remain separate subsystems.

```text
Investigation
    ↓
Diagnosis
    ↓
Remediation Planner
    ↓
Human Approval
    ↓
Execution Engine
    ↓
Verification
```

The planner should not automatically execute.

The execution engine should expose typed, constrained actions.

Prefer:

```text
scale_deployment(service, replicas)
restart_deployment(service)
rollback_deployment(service, revision)
```

over:

```text
execute_arbitrary_shell(command)
```

## 10. Authorization Boundary

The eventual system should distinguish:

### Read
Collect information.

### Plan
Generate a proposed fix.

### Execute
Modify the environment.

Production execution requires explicit authorization and policy checks.

Potential policy model:

```text
Restart unhealthy pod
→ Allowed automatically or low-risk approval

Scale deployment
→ Approval required

Rollback production
→ Strong approval

Database mutation
→ Restricted / human-only initially
```

Exact policy rules are future product decisions.

## 11. Verification

A successful command is not equivalent to successful remediation.

The execution flow should be:

```text
Execute
   ↓
Observe
   ↓
Compare actual vs expected state
   ↓
Resolved / Not resolved
```

Verification should use objective signals such as:
- Pod health
- Error rate
- Resource utilization
- Restart count
- Service health
- Application-specific health checks

## 12. Future SaaS Architecture

The eventual service should conceptually separate:

### Control Plane

```text
Tenant management
Incident management
Investigation orchestration
Evidence/timeline
RCA
Remediation planning
Policies
Audit
Slack/UI/API
```

### Customer Execution Plane

```text
Customer connector
   ↓
Customer Kubernetes/cloud/observability systems
```

The customer connector should initially be read-only.

Later it can expose narrowly scoped write capabilities.

## 13. Tenant Isolation

When the system becomes multi-tenant:
- Every request must carry tenant context.
- Customer data must be isolated.
- Credentials/integrations must be tenant-scoped.
- Audit records must be tenant-scoped.
- Access control must be enforced at API/service/data boundaries.

Do not treat tenant isolation as merely a UI concern.

## 14. Observability of the Investigator

The platform itself should be observable.

Eventually capture:
- Investigation duration
- Tool/capability calls
- Evidence sources used
- Investigation decisions
- Model calls
- Token/cost metrics where relevant
- Diagnosis confidence
- Human overrides
- Remediation actions
- Verification results

The investigation trace is important for debugging and customer trust.

## 15. Incremental Evolution Rule

Do not rewrite the working Iteration-2 investigator simply to match the future architecture.

Refactor incrementally:

```text
Existing Kubernetes Investigator
        ↓
Extract capability interfaces
        ↓
Kubernetes capability adapter
        ↓
Preserve existing diagnosis engine
        ↓
Add adaptive planner
        ↓
Add additional provider
```

Existing tests should remain regression tests throughout.

## 16. Local Prototype Architecture

The immediate local prototype may remain simple:

```text
Local Kubernetes
   ├── Application
   ├── PostgreSQL
   └── Failure Injection

Evidence Sources
   ├── Kubernetes API/events
   ├── Application logs
   └── Metrics where available

Investigation System
   ├── Incident context
   ├── Capability layer
   ├── Evidence collection
   ├── Diagnosis
   ├── Timeline
   └── Report

Communication
   └── Slack
```

This is intentionally much smaller than the final SaaS architecture.

The local prototype is a vertical slice used to prove the investigation capability before adding SaaS complexity.

## 17. Architectural Non-Goals for Current Prototype

Do not add yet:
- Multi-tenancy
- Billing
- Full SaaS control plane
- Multiple cloud providers
- Autonomous remediation
- Arbitrary production command execution
- Large numbers of integrations
- Complex UI unless needed for demonstration

These belong to later milestones in `ROADMAP.md`.

## 18. Actual Implementation Map (as of Iteration 4, branch 1: evidence retention)

The architecture above as it exists in the repository (there is no `src/`; the investigator is the
`investigator/` package):

```text
Detection            investigator/detector.py            symptoms only, via capabilities
Incident context     investigator/context.py             signals + window + suspect components
Planner              investigator/planner.py             decides what to examine next; decisions traced
Capability layer     investigator/capabilities/          interface (base.py), traced facade (__init__.py)
  adapters           capabilities/kubernetes.py          Kubernetes (the only resource provider so far)
                     capabilities/prometheus.py          metrics (optional)
  evidence history   capabilities/kubernetes_recorder.py records what Kubernetes forgets (provider side)
                     investigator/history_store.py       provider-neutral SQLite store (state/history.db)
Composition root     investigator/providers.py           the one place that chooses adapters
Evidence             collect.py, dependencies.py, metrics.py, logparse.py -> evidence.py (facts)
Diagnosis            investigator/diagnosis.py            neutral causes/categories -> findings -> root cause
Report               investigator/report.py, slack.py    text / markdown / json / Slack
```

`tests/test_architecture.py` enforces three boundaries:
- **§2 boundary.** It fails if a provider-independent module imports provider code. This includes the
  history store and the recorder: the engine reaches retained evidence only through capabilities.
- **Vocabulary.** It fails if reasoning code uses Kubernetes vocabulary.
- **Neutral store.** The history store must not depend on Kubernetes or its vocabulary.

**Evidence history** (Iteration 4).

How it is recorded and stored:
- The recorder runs inside `watch`, or alone as `record`.
- It watches pods and polls logs, events, ConfigMaps, Secret fingerprints and workload definitions.
- The store keeps per-run logs, lifecycle, events, object versions and recording sessions.
- Retention is 24 h by default. Log lines over a per-minute cap are dropped, and the drop is counted.

How the adapter serves it:
- Earlier runs and deleted instances: `get_resource_state`, `get_log_history`.
- Expired events: `get_events`.
- Recorded configuration and definition changes: `get_configuration_history`.
- Recording sessions: `get_evidence_coverage`. Coverage gaps become `evidence_gap` facts and are listed in
  the report.

Known limits:
- Nothing is retained while the recorder is not running.
- Lines a pod writes in its last ~5 s before it is deleted are lost.
- Prometheus keeps its own data on an `emptyDir`, so metrics are lost if its pod is recreated.

Kubernetes is the only resource provider. Provider agnosticism is not claimed until a second provider
exercises the same interface (Iteration 8).
