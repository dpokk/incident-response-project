# ROADMAP.md

## Current Position

| Milestone | State |
|---|---|
| Iteration 1 — OOM detection + LLM report | Complete |
| Iteration 2 — Generalized evidence-driven investigation | Complete |
| Stabilization after Iteration 2 | Complete |
| Iteration 3 — Capability-based investigation | Complete |
| Iteration 4 — Historical evidence & incident reconstruction | **In progress** |
| Iterations 5–11 | Planned |

The project is currently at:

> Capability-based, evidence-driven incident investigation and reporting, proven on Kubernetes (Iteration 3
> complete). In progress: Iteration 4, historical evidence and incident reconstruction.

The project is NOT currently a production SaaS platform and does NOT currently execute remediation.

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

### Stabilization after Iteration 2 — COMPLETED

Done on branch `stabilization/post-iteration-2` and merged to `main` together with Iteration 3.

Corrections found when the project context documents were reconciled with the code:

| Item | Resolution |
|---|---|
| Iteration numbering drift | Docs, README and `.env.example` follow this roadmap: Iteration 3 is capabilities, Iteration 4 is observability + Slack. |
| Iteration 1 under-described | Iteration 1 section above corrected. |
| Traffic-spike scenario | Marked **partially implemented** in `docs/INCIDENTS.md` (Incident 5). |
| Scenario C representation | Reports now give a **root-cause component** (postgres) separately from the **affected component** (backend) and the **impacted components** (frontend). |
| Timestamp precision | Events that began before the window keep their real first timestamp and are marked `before_window`; they are never re-dated to the window start (`tests/test_timeline.py`). |
| Similar-symptom tests | `tests/test_similar_symptoms.py`: 4 cases in this pass, now 5 (the fifth was added by the Iteration 3 final demo fixes, below). These found and fixed a real gap: errors naming a dependency by IP address were not linked to it. |
| Repository structure | Documented in `docs/ARCHITECTURE.md` §18 and `CLAUDE.md`. |
| Report contract: quantified impact and recommendations | **Decided** (below). |

#### Decided: impact and recommendations in the report contract

Reports list impacted components but no **quantified impact** (failed requests, error rate, impact
duration) and no **recommendations**.

Decision, confirmed on 2026-09-30:
- **Quantified impact / blast radius → Iteration 4.** It depends on the richer observability that
  iteration brings, such as retained metrics and logs.
- **Recommendations → Iteration 5 (remediation planning).** Putting them into diagnosis reports would blur
  the diagnosis/remediation boundary in `docs/ARCHITECTURE.md` §9.
- Iteration 3 scope is not changed retroactively for this, only for a correctness issue.

### Iteration 3 — COMPLETED

#### Goal

Move from a deterministic multi-scenario investigator toward a capability-based investigation architecture that can eventually support AI-assisted/adaptive investigation.

#### Desired flow

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

#### First architectural abstraction

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

#### Status — COMPLETED (merged to `main` together with the stabilization pass)

| Step | State | What exists |
|---|---|---|
| 1. Capability interface + Kubernetes adapter | Done | `investigator/capabilities/` (`base.py` interface and records, `kubernetes.py`, `prometheus.py`). Investigation modules no longer import provider code. Facts were verified byte-identical to Iteration 2 on all fake clusters. |
| 2. Incident context + investigation planner | Done | `investigator/context.py`, `investigator/planner.py`: deterministic and evidence-driven, every decision traced with its reason. The Iteration 2 procedure is kept as `INVESTIGATION_STRATEGY=exhaustive`. |
| 3. Detection on capabilities | Done | `detector.py` uses only `ResourceProvider` / `MetricsProvider`. Optional `reset()` and `start_background_recording()` hooks; the Kubernetes pod journal now sits behind the adapter. `providers.py` is the composition root. |
| 4. Provider-neutral fact vocabulary | Done | Neutral termination causes, event categories and waiting causes, mapped in the adapter. Neutral fact kinds (`component_status`, `process_terminated`, `event`, …). `legacy.py` replays older evidence. A test forbids Kubernetes vocabulary in reasoning code. Diagnoses are identical before and after. |

Iteration 3 is complete.

Not part of it, and still planned:
- an LLM-assisted planner;
- a second provider (Iteration 8);
- quantified impact (Iteration 4) and recommendations (Iteration 5), as decided above.

Success criteria as currently evidenced:

| Criterion | Evidence |
|---|---|
| 1. Evidence via capabilities | `tests/test_architecture.py`, which also covers detection and the neutral vocabulary |
| 2. Four scenarios still work | Fake-cluster tests, plus a live run on 2026-09-30 through the planner (below) |
| 3. Tests green | 37/37 (after the stabilization pass and the final demo fixes) |
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

#### Final demo fixes

Done on branch `fix/final-run-findings` and merged to `main`.

The closing live demo on 2026-09-30 exposed two defects. Both were fixed and re-verified live, and the OOM
scenario again gave memory exhaustion in backend.

1. **Root cause attributed to the caller.**
   - Problem: with the backend killed for exceeding its memory limit and fully down, the frontend's
     "backend unavailable" finding outscored the backend's own memory finding, so the report blamed the
     frontend.
   - Fix: a caller's dependency finding now counts as an effect whenever the component it calls has a
     well-supported failure of its own (score ≥ 0.4). The two scores are no longer compared.
   - Test: `world_oom_backend_dead` in `tests/test_similar_symptoms.py`.
2. **Crash evidence lost for deleted instances.**
   - Problem: a pod that crashed and was replaced before the investigation ran disappeared from the
     evidence.
   - Fix: the adapter also returns recorded terminations of the component's deleted instances
     (`instance_gone`). The report says their logs are unavailable and gives a low-confidence crash
     diagnosis instead of "Undetermined".
   - Test: `tests/test_adapter.py`.

#### Known limitations at completion

- **Historical Slack reports.**
  - The Slack channel holds two reports from the final demo run, posted before these fixes:
    - INC-20260930-231441 wrongly reports the OOM scenario as "dependency unavailable" in frontend.
    - INC-20260930-230953 is "Undetermined" for the crash whose pod had already been deleted.
  - They are kept as historical prototype output; no corrections were posted.
- **Unexplained backend exit.**
  - During PostgreSQL recovery in that run, one backend instance exited with code 1.
  - A targeted retry did not reproduce it, and the cause is unknown.
  - The investigator now reports it as an application crash whose cause could not be determined, with low
    confidence.
- **Log retention across restarts.** This causes the lower OOM confidence above; it is planned for
  Iteration 4.
- **Single provider.** Kubernetes is the only resource provider, so provider agnosticism is not claimed
  (Iteration 8).

#### Success Criteria

Iteration 3 should demonstrate that:
1. The investigator can request evidence through capabilities rather than directly depending on Kubernetes implementation details.
2. Existing four scenarios continue to work.
3. Existing offline tests remain green or are updated appropriately.
4. Evidence remains traceable.
5. The system can decide which evidence/capabilities are relevant to an incident.
6. The final diagnosis remains evidence-driven.

## Iteration 4 — Historical Evidence & Incident Reconstruction — IN PROGRESS

Defined on 2026-10-01. This replaces the earlier draft, "Richer Observability + Slack Workflow". The Slack
goals of that draft (Slack as a first-class incident interface) are deferred to a later stage and are not
yet scheduled.

### Why

Iteration 3 reasons well over evidence that exists at investigation time, but evidence disappears with the
workload. In the live OOM run the instance that took the traffic spike was several restarts back.
Kubernetes keeps only the current and previous container logs, so the investigator saw only a 5-second
instance that never logged its memory warning. Confidence was 70%, and the missing evidence, not the
reasoning, was the limit.

### Objective

Make incident evidence durable enough that the investigator can reconstruct an incident after the original
failing workload no longer exists. This is an evidence and history iteration: no remediation, no LLM, no
Slack workflow.

### Scope

1. **Historical evidence retention**, beyond the lifetime of Kubernetes objects:
   - logs, including earlier restarts and deleted instances;
   - terminations and restart history;
   - lifecycle and Kubernetes events;
   - deployment and configuration changes;
   - metrics, which stay in Prometheus.
2. **Incident evidence window.** Before incident → onset → development → failure → recovery → post-incident.
   Facts distinguish event time, observation time, collection time and uncertainty. No invented
   timestamps.
3. **Historical timeline reconstruction** through the capability layer.
   - It answers: what changed first, when symptoms began, when the failure occurred, what preceded it, and
     what followed recovery.
   - Observed facts stay separate from inferred relationships.
4. **OOM after the failing instance is gone**, as the primary validation. Confidence may rise only because
   more evidence genuinely exists. When evidence is missing, the report says so.
5. **Metrics correlated with other evidence**, e.g. traffic → memory → kill. A connection is made only when
   ordering and component support it; there is no "spike = OOM" rule.
6. **Basic evidence-backed impact assessment.**
   - It covers affected instances, dependency health, impacted callers, duration where known, request and
     error impact where measurable, and whether the impact was isolated or propagated.
   - Quantities are never invented.
   - Root cause, affected, impacted and blast radius stay distinct.

The Iteration 3 capability/planner architecture is preserved. The planner stays deterministic, and
Kubernetes-specific code stays in the adapter layer.

### Design decisions

| Decision | Why |
|---|---|
| **A local SQLite history store** (`state/history.db`) | Evidence must be looked up by component and time while the recorder is writing, and pruned. SQLite does that with the standard library and a single file. It is also a fitting local buffer for the future customer connector (Iteration 10). |
| **A recorder in the investigator process** (started by `watch`, or alone with `record`) | It extends the existing pod journal: no new image, permissions or deployment. It is a separate module behind the adapter, so it can later move into a connector. |
| **Retention bounds:** 24 h by default, plus a per-container cap on log lines per minute | Without a bound, a spike writes about 85,000 frontend lines in 5 minutes. When lines are dropped, the store records a marker, so the loss is reported as uncertainty. |
| **Coverage reporting** | Nothing is retained while the recorder is not running. Each source reports the time span it can see, and the report states the gaps. |
| **No persistent volume for Prometheus** | Its `emptyDir` already outlives application pods and survives minikube stop/start. Losing metrics when the Prometheus pod itself is recreated is a recorded limitation. |

### Progress (2026-10-01)

All five planned branches are merged into `main`. The completion audit found two gaps, and a finalization pass
addresses them; it is pending approval:
- `fix/iter-04-uncertain-timestamps` — bounded times were shown as exact.
- `feature/iter-04-historical-dependency-state` — a recovered dependency outage could not be reconstructed.

Live validation (`docs/validation/iter-04-oom-postmortem.md`), each investigated after the original failure
was no longer visible in live state:

| Incident | Without history | With history |
|---|---|---|
| OOM, pods replaced | Undetermined (0%) | Memory exhaustion (85%) |
| OOM, crash-looping pods (the Iteration 3 problem) | Memory exhaustion (70%) | Memory exhaustion (85%); the memory warning comes from the retained 587 s run |
| PostgreSQL down, then recovered before investigation | **Application crash (75%) — wrong** | **Dependency unavailable; root cause postgres, affected backend, impacted frontend** (57%: a real backend crash during the recovery is the runner-up) |

Recorded as limitations and backlog, not Iteration 4 work:
- Confidence scores are heuristic and not calibrated.
- There is no automated live regression.
- Metrics sampling is sparse, and live traffic → memory → OOM causality remains unproven.
- Readiness transitions are recorded but not consumed.
- Some capabilities remain Kubernetes-shaped.
- Interpretation is growing inside `report.build()`.
- The recorder is client-side and must be running.
- Thresholds have been tuned against the demo application.
- Overlapping incidents need better separation.
- Connectivity is not retained historically: availability comes from endpoint and replica counts.
- The demo backend can crash with an unhandled `_queue.Empty` after a `UniqueViolation` when PostgreSQL comes
  back. This was the "unexplained exit" since Iteration 3. It is a demo application bug, and it is reported by
  the investigator as a separate finding.

### Planned branches

| Branch | Purpose |
|---|---|
| `feature/iter-04-historical-evidence-retention` | History store, recorder, time/provenance fields, coverage; the adapter serves retained evidence |
| `feature/iter-04-timeline-reconstruction` | Phased timeline, inferred relations, incident evidence window |
| `feature/iter-04-metrics-correlation` | Metrics of instances that no longer exist, threshold crossings, correlation checked by ordering |
| `feature/iter-04-impact-assessment` | Impact and blast radius in the report |
| `feature/iter-04-oom-postmortem-validation` | Scenario script, offline world, live validation |

### Not part of Iteration 4

- A second provider, or true provider agnosticism.
- LLM-based investigation.
- Remediation, approval workflows, or any autonomous change.
- Elaborate Slack workflows: Slack may only show the improved report.
- SaaS, multi-tenancy, business-impact analytics.

### Success criteria

1. Historical evidence survives workload/container disappearance.
2. A post-failure investigation can reconstruct an incident from retained evidence.
3. The OOM scenario can be investigated after the failing instance is gone.
4. The timeline combines multiple evidence sources without fabricating timestamps.
5. Metrics support incident reconstruction when relevant.
6. The report contains a basic evidence-backed impact assessment.
7. The capability/planner architecture remains intact.
8. Missing evidence produces explicit uncertainty, not fabricated conclusions.
9. All existing tests remain green, and new tests cover these cases:
   - pod gone;
   - restarted container;
   - OOM;
   - timeline uncertainty;
   - metrics correlation;
   - impact;
   - missing evidence.
10. No remediation or production-changing behaviour is introduced.

## Iteration 5 — Remediation Planning

### Goal

Given a diagnosed incident, generate a proposed remediation plan. Recommendations are introduced here, not
in diagnosis reports (report contract decision).

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
