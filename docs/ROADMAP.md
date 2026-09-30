# ROADMAP.md

## Current Status

### Iteration 1 — COMPLETED
**Goal:** Detect a Kubernetes OOMKilled incident.

The original prototype did more than OOM detection (code archived in `archive/iteration1/`):
- a Python investigator that detected a traffic-spike → OOMKilled incident;
- a Prometheus-based T1–T8 timeline (traffic onset → CPU/memory saturation → OOMKill → 5xx → recovery);
- quantified impact (failed requests, duration);
- an LLM-written narrative (Claude; later NVIDIA-hosted models) with recommendations;
- Slack posting.

### Iteration 2 — COMPLETED
**Goal:** Generalize the investigator into an evidence-driven multi-scenario investigation system.

Implemented:
- PostgreSQL dependency
- Four live failure scenarios:
  1. OOMKilled / memory exhaustion
  2. Database configuration/connectivity failure
  3. PostgreSQL unavailable
  4. Application crash
- Scenario-blind investigation: the investigator is not told which scenario was injected.
- Evidence collection from:
  - Pod/container state
  - Kubernetes events
  - Current and previous logs
  - Services and endpoints
  - Configuration
  - Dependencies
  - Deployment/ConfigMap changes
  - Optional metrics
- Evidence-backed diagnosis.
- Fact vs interpretation separation.
- Root cause vs impacted component distinction.
- Dependency-aware investigation.
- Alternative/rejected explanation handling.
- Structured incident reports.
- Timeline reconstruction.
- Investigation trace.
- Fake Kubernetes environment for offline testing.
- 7/7 offline tests passed at the end of Iteration 2. These remain the regression baseline inside the
  larger suite.
- All four failure scenarios tested live.

Iteration 1 capabilities intentionally not carried into Iteration 2 (not yet restored):
- the LLM narrative and its recommendations;
- quantified impact / blast radius (failed requests, impact duration);
- Prometheus-based T1–T8 timeline reconstruction. Iteration 2's timeline lists timestamped facts; it
  does not derive threshold crossings.

### Deliberately NOT Implemented in Iteration 2
- Automated remediation
- LLM/AI investigator
- Autonomous production actions
- Redis/Kafka additions
- New observability stack
- SaaS/multi-tenancy
- Provider-agnostic multi-provider implementation

## Current Position

The project is currently at:

> Generalized, evidence-driven Kubernetes incident investigation and reporting.

The project is NOT currently a production SaaS platform and does NOT currently execute remediation.

## Immediate Next Milestone — Iteration 3

### Goal

Move from a deterministic multi-scenario investigator toward a capability-based investigation architecture that can eventually support AI-assisted/adaptive investigation.

### Desired flow

```text
Incident
   ↓
Incident Context
   ↓
Investigation Planner
   ↓
Select Required Capabilities
   ↓
Collect Evidence
   ↓
Evaluate Evidence
   ↓
Decide What to Investigate Next
   ↓
Timeline + Diagnosis
   ↓
Structured Report
   ↓
Slack
```

### First architectural abstraction

Introduce provider-independent capabilities such as:

- `get_logs()`
- `get_metrics()`
- `get_service_health()`
- `get_resource_state()`
- `get_events()`
- `get_dependencies()`
- `get_configuration()`
- `get_deployment_history()`

Kubernetes should implement these capabilities through an adapter.

Do not immediately rewrite the working investigator. Refactor incrementally.

### Status — IN PROGRESS (branch `iteration-3/capability-based-investigation`, not yet merged to `main`)

| Step | State | What exists |
|---|---|---|
| 1. Capability interface + Kubernetes adapter | Done | `investigator/capabilities/` (`base.py` interface and records, `kubernetes.py`, `prometheus.py`). Investigation modules no longer import provider code. Facts were verified byte-identical to Iteration 2 on all fake clusters. |
| 2. Incident context + investigation planner | Done | `investigator/context.py`, `investigator/planner.py`: deterministic and evidence-driven, every decision traced with its reason. The Iteration 2 procedure is kept as `INVESTIGATION_STRATEGY=exhaustive`. |
| 3. Detection on capabilities | Done | `detector.py` uses only `ResourceProvider` / `MetricsProvider`. Optional `reset()` and `start_background_recording()` hooks; the Kubernetes pod journal now sits behind the adapter. `providers.py` is the composition root. |
| 4. Provider-neutral fact vocabulary | Done | Neutral termination causes, event categories and waiting causes, mapped in the adapter. Neutral fact kinds (`component_status`, `process_terminated`, `event`, …). `legacy.py` replays older evidence. A test forbids Kubernetes vocabulary in reasoning code. Diagnoses are identical before and after. |

Iteration 3 is architecturally complete once these steps are merged.

Success criteria as currently evidenced:

| Criterion | Evidence |
|---|---|
| 1. Evidence via capabilities | `tests/test_architecture.py`, which also covers detection and the neutral vocabulary |
| 2. Four scenarios still work | Fake-cluster tests, plus a live run on 2026-09-30 through the planner (below) |
| 3. Tests green | 34/34 (after the stabilization pass) |
| 4. Evidence traceable | Every capability call is traced with its provider |
| 5. Relevance decided | Planner decisions with reasons; `tests/test_planner.py` |
| 6. Diagnosis evidence-driven | Unchanged diagnosis engine; planned and exhaustive strategies agree |

Live run on 2026-09-30: all four scenarios were diagnosed correctly through the planner, with the same
categories as in Iteration 2.

| Scenario | Diagnosis | Confidence |
|---|---|---|
| Database misconfiguration | Dependency misconfiguration | 97% |
| PostgreSQL down | Dependency unavailable | 97% |
| Application crash | Application crash | 90% |
| OOM | Memory exhaustion | 70% (85% in the Iteration 2 run) |

The OOM confidence difference is evidence availability, not the planner:
- The planner did read the previous-instance logs.
- The crash-looping instance lived about 2 s, so it never logged its memory warning.
- Kubernetes keeps only one previous instance's logs.

Retaining logs across restarts is an Iteration 4 (observability) concern.

### Success Criteria

Iteration 3 should demonstrate that:
1. The investigator can request evidence through capabilities rather than directly depending on Kubernetes implementation details.
2. Existing four scenarios continue to work.
3. Existing offline tests remain green or are updated appropriately.
4. Evidence remains traceable.
5. The system can decide which evidence/capabilities are relevant to an incident.
6. The final diagnosis remains evidence-driven.

## Stabilization pass — DONE (branch `stabilization/post-iteration-2`, merged into Iteration 3)

Corrections found when the project context documents were reconciled with the code:

| Item | Resolution |
|---|---|
| Iteration numbering drift | Docs, README and `.env.example` follow this roadmap: Iteration 3 is capabilities, Iteration 4 is observability + Slack. |
| Iteration 1 under-described | Iteration 1 section above corrected. |
| Traffic-spike scenario | Marked **partially implemented** in `docs/INCIDENTS.md` (Incident 5). |
| Scenario C representation | Reports now give a **root-cause component** (postgres) separately from the **affected component** (backend) and the **impacted components** (frontend). |
| Timestamp precision | Events that began before the window keep their real first timestamp and are marked `before_window`; they are never re-dated to the window start (`tests/test_timeline.py`). |
| Similar-symptom tests | `tests/test_similar_symptoms.py`, 4 cases. These found and fixed a real gap: errors naming a dependency by IP address were not linked to it. |
| Repository structure | Documented in `docs/ARCHITECTURE.md` §18 and `CLAUDE.md`. |
| Report contract: quantified impact and recommendations | **Open decision** (below). |

### Open decision: impact and recommendations in the report contract

Reports currently list impacted components but no **quantified impact** (failed requests, error rate,
impact duration) and no **recommendations**.

Recommendation, pending confirmation:
- **Quantified impact → Iteration 4.** It depends on the richer observability that iteration brings,
  such as retained metrics and logs.
- **Recommendations → Iteration 5 (remediation planning).** Putting them into diagnosis reports now
  would blur the diagnosis/remediation boundary in `docs/ARCHITECTURE.md` §9.

## Iteration 4 — Richer Observability + Slack Workflow

### Goal

Make logs, metrics, Kubernetes events, resource state, configuration, and dependencies usable as a coherent evidence system and make Slack a first-class incident interface.

Desired Slack output:
- Incident summary
- Root cause
- Evidence
- Timeline
- Impact
- Confidence
- Link/view for detailed investigation

Do not introduce remediation execution yet.

## Iteration 5 — Remediation Planning

### Goal

Given a diagnosed incident, generate a proposed remediation plan.

The plan must include:
- Current state
- Root cause
- Proposed actions
- Expected final state
- Risks
- Verification criteria
- Rollback approach

The system must NOT execute the plan automatically.

## Iteration 6 — Human-Approved Remediation

### Goal

Allow an engineer to explicitly approve a proposed remediation.

Workflow:

```text
Diagnosis
   ↓
Plan
   ↓
Engineer reviews
   ↓
Approve / reject
   ↓
Policy check
   ↓
Execute approved typed actions
```

Start with a small, safe set of Kubernetes actions.

Potential initial actions:
- Restart deployment
- Scale deployment
- Rollback deployment

Avoid arbitrary shell execution.

## Iteration 7 — Remediation Verification

### Goal

Verify that remediation produced the expected final state.

Workflow:

```text
Execute
   ↓
Observe
   ↓
Compare actual state vs expected state
   ↓
Resolved / Not resolved
```

If verification fails, the system should not blindly continue executing changes. It should return to investigation or request human intervention.

## Iteration 8 — Provider Abstraction + Second Provider

### Goal

Prove that the same investigation engine can work beyond Kubernetes.

Add a second provider/infrastructure environment, likely AWS because of the project's existing AWS prototype.

Architecture:

```text
Investigation Engine
        ↓
Capability Interfaces
        ↓
 ┌──────┴───────┐
 ↓              ↓
Kubernetes     AWS
Adapter        Adapter
```

Success criterion:
The same provider-independent investigation logic can diagnose representative incidents through both implementations.

## Iteration 9 — SaaS Control Plane

### Goal

Turn the system into a service usable by multiple organizations.

Introduce:
- Organizations/tenants
- Users
- Teams
- Authentication
- Authorization/RBAC
- Environments
- Integrations
- Incident history
- Audit logs
- Tenant isolation
- Usage/account management as needed

Customer data and configuration must be isolated by tenant.

## Iteration 10 — Customer Connector

### Goal

Allow an organization to connect its environment without giving the SaaS unrestricted network access.

Initial connector:
- Read-only
- Runs in/near customer environment
- Exposes controlled capabilities
- Communicates securely with the control plane

Later:
- Add narrowly scoped write capabilities for remediation.

## Iteration 11 — External Pilot

### Goal

Test the platform with 2–3 real engineering organizations/design partners.

Initial pilot should focus on:
- Incident investigation
- Evidence quality
- Root-cause accuracy
- Timeline usefulness
- Slack workflow

Remediation should initially be approval-only.

Use pilot feedback to determine:
- Most valuable integrations
- Most common incident types
- Useful evidence sources
- Safe remediation actions
- False-positive/false-root-cause patterns

## Important Sequencing Rules

1. Do not jump to SaaS before the investigation abstractions are stable.
2. Do not jump to autonomous remediation before remediation planning and verification are reliable.
3. Do not claim provider agnosticism until a second provider has actually exercised the same abstraction.
4. Do not replace the deterministic evidence/validation layer with an LLM.
5. Do not add many integrations merely for breadth; prioritize integrations that prove the architecture or are demanded by pilots.
6. Preserve the current four Kubernetes scenarios as regression tests throughout future iterations.

## Long-Term Direction

The intended end state is:

```text
Customer Environment
        ↓
Incident
        ↓
Context
        ↓
Adaptive Investigation
        ↓
Evidence
        ↓
Timeline + Root Cause + Blast Radius
        ↓
Slack
        ↓
Remediation Plan
        ↓
Human Approval
        ↓
Controlled Execution
        ↓
Verification
        ↓
Resolved
```
