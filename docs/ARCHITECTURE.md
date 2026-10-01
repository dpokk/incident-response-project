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
- **Report wording.** A time is never printed more precisely than it is known:
  - an exact time prints as one time;
  - a bounded time prints as its range, `HH:MM:SS–HH:MM:SS`;
  - an observed-only time prints as "at or before HH:MM:SS";
  - a pre-window event prints as "before window".
- **Moments.** Phase boundaries and the incident start and end are moments `{earliest, latest, basis}`, not
  single numbers.
- **Durations are bounds.** The shortest is end.earliest − start.latest and the longest is
  end.latest − start.earliest. So durations read "between A and B", "at least A" (ongoing, or the start may be
  earlier) or a single value only when both ends are exact. An ordering gap is "at least N s" unless both ends
  are exact.
- **`observed_at` on facts.** It records when the source saw the evidence: the recorder for retained
  evidence, the platform's last sighting for aggregated events. For a live read, `observed_at` is empty and
  `collected_at` is the observation. Event time, observation time and collection time therefore stay distinct
  in facts and in the report JSON.
- **Reconstruction** (`investigator/timeline.py`, Iteration 4) builds three layers:
  - **Observed entries.** Facts with a role (change, precursor, symptom, failure, context) and a time
    interval taken from their time basis.
  - **Inferred phases.** Baseline → onset → development → failure → recovery → after, each citing its
    facts.
  - **Inferred ordering.** "A preceded B" only when the intervals do not overlap. Ordering is never cause.
- **The questions it answers:**
  - **What changed first:** the first change in the lead-up, otherwise the latest earlier change with its
    age.
  - When symptoms began.
  - The earliest *recorded* failure.
  - What happened just before it.
  - What happened after recovery.
- **Rules that keep it honest:**
  - Recovery is claimed only when every instance of the failing components is ready, became ready after
    the last failure (by the source's own timestamps), and has stopped logging errors.
  - Restarts that outnumber recorded terminations, and back-off seen before the first recorded failure, are
    stated as unrecorded earlier failures.
  - An observation aggregated since before the window is never used as the onset, because its first
    sighting may belong to an earlier episode.
- **Impact** (`investigator/impact.py`) reports, separately from the root cause:
  - affected instances, counting those that no longer exist and ignoring instances created after the
    window;
  - dependency state ("healthy" only with evidence);
  - impacted callers;
  - users, with failed requests *estimated* from sampled rate × error ratio when metrics allow, otherwise
    stated as not quantified;
  - duration, propagation (isolated, propagated or unknown) and blast radius.
- **Overlapping incidents.** One window can hold two incidents:
  - each error episode is its own fact, and episodes that ended before the failure began are not counted
    as its impact;
  - log signatures split into bursts at silences longer than 120 s;
  - observations that stopped before the failure are not its onset.
- **Past windows.** Evidence, recovery and instances after the investigated window are not borrowed from
  today's state.
- **Metric threshold crossings.** These are timeline entries with bounded times (`investigator/metrics.py`).
  A crossing lies between two samples, widened by the averaging window of rates. A user-facing error ratio
  falling back below 5% supports the recovery phase.
- **Limit.** "First change" means first in order, not relevant. Relevance is the diagnosis' job, so an
  unrelated change inside the window (e.g. another operation) is listed but not blamed.
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

**As implemented (Iteration 5, the planner only).**

```text
diagnosis + reconstruction + impact + facts  →  plan_remediation()  →  RemediationPlan  →  STOP
                                                 (investigator/remediation.py)       (remediation_model.py)
```

- **Placement.** The planner is a pipeline stage after the report, in its own module; it is not inside
  `report.build()`. It receives the finished investigation and **no capabilities object or provider**. It
  cannot read or change the system, and an architecture test forbids imports of providers, `subprocess`, `os`,
  the network or Slack.
- **Diagnosis-driven.** It copies the diagnosis (category, root cause, affected and impacted components,
  confidence) and never re-derives the cause.
- **Generic eligibility.** Each action type has an eligibility rule over neutral evidence predicates, not over
  the incident category:
  - `adjust_resource_limit`: the root-cause component was terminated at a resource limit.
  - `scale_workload`: the component behind a failing dependency has zero desired replicas *now*, or a load
    increase is *linked* to the failure.
  - `restore_configuration`: the root cause is a configuration item and the endpoint it names still does not
    resolve now.
  - `investigate_further`: never executable; it states what to examine and the evidence that raises it.
- **Urgency from the current state.**
  - `immediate` means the condition holds now;
  - `preventive` means it held only during the incident;
  - moot conditions produce no action.

  A recovered incident therefore gets no immediate change unless a current condition still requires one.
- **Corrective actions require a diagnosis that is not Low confidence.** Otherwise, and when no typed action
  fits, the plan says so (`investigate_further`, `no_safe_action`) instead of manufacturing an action. Values
  the evidence cannot supply (a new memory limit, a replica count) stay empty with
  `parameters_complete: false`.
- **Contract.** `RemediationPlan` (`schema_version` "1") contains:
  - the diagnosis summary;
  - `incident_state` and the current state, each with fact IDs;
  - the assessment and its reason;
  - typed actions, each with rationale, preconditions, expected final state, risks, rollback and
    verification;
  - uncertainty: the diagnosis' confidence, competing causes and evidence gaps;
  - an evidence snapshot;
  - `requires_human_approval: true`;
  - `approval.status: awaiting_review`;
  - `execution.status: not_executed`.
- **Verification criteria use the capability vocabulary,** so Iteration 7 can evaluate them:
  `component_ready`, `no_new_terminations`, `dependency_available`, `no_dependency_errors`,
  `entry_requests_succeed`, `error_ratio_below`.
- **Presentation.** `plan.to_dict()` is plain, deterministic JSON. It is saved as `INC-*.plan.json` and
  embedded in the report JSON. The text and markdown reports render it from the dict. Slack, a UI or an API
  (Iteration 6) consume the same dict: **Slack consumes the contract; it does not define it.**

**As implemented (Iteration 6, human review; nothing executes).**

```text
RemediationPlan dict ─→ review.register_plan() ─→ digest (sha256 of canonical JSON)
        │                    (review.py, state/reviews.db)
        └─→ slack_view (render) ─→ slack.Transport ─→ incident thread (root + investigation + plan)
Slack click ─→ Socket Mode ─→ slack_app.handle_interaction() ─→ review.decide() ─→ decision recorded
                                                              └─→ thread updated ─→ STOP
```

- **Layers.**
  - `slack_view.py` is presentation only.
  - `review.py` is the Slack-independent review model (stdlib only; a Slack ID is just a reviewer string).
  - `slack.py` is the transport and the thread registry (`state/slack_threads.json`).
  - `slack_app.py` translates an interaction into `decide()` and shows the result.

  Architecture tests forbid these modules to import providers, capabilities, `subprocess` or the planner, and
  forbid the planner to import Slack or the review.
- **Decision record.** Each record holds the incident, the plan digest, the action index and type, the
  decision, the reviewer, the supplied parameters, the comment, the time, `effective` and the refusal reason.
  Refused attempts are audited too.
- **Rules.**
  - Only `SLACK_APPROVERS` may decide.
  - Decisions are per action, and the allowed set depends on the action type.
  - The first effective decision stands.
  - A newer plan supersedes the older one and its decisions.
  - Required values are typed by the engineer and validated; a candidate from the evidence is never used
    implicitly.
- **Authorisation boundary (§10).** Approval is a recorded human decision, not an execution trigger. Nothing in
  Iteration 6 reads an approval to change the system; that is Iteration 7, behind a policy check.

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

## 18. Actual Implementation Map (as of Iteration 6, complete)

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
Timeline             investigator/timeline.py            phases, ordering, answers, uncertainty (inferred)
Impact               investigator/impact.py              affected instances, dependencies, impacted, users,
                                                         duration, propagation, blast radius (evidence-backed)
Remediation plan     investigator/remediation.py         deterministic planner; no capabilities, no execution
                     investigator/remediation_model.py   RemediationPlan contract (imports nothing else)
Report               investigator/report.py              text / markdown / json
Human review         investigator/review.py              decisions bound to a plan digest (state/reviews.db)
Slack                investigator/slack_view.py          Block Kit rendering (presentation only)
                     investigator/slack.py               transport, one thread per incident
                     investigator/slack_app.py           Socket Mode interactions -> review.decide()
```

`tests/test_architecture.py` enforces these boundaries (plus the remediation and human-review boundaries of §9):
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

Availability is also recorded, for dependency outages that recover before the investigation:
- Ready endpoints per Service and ready replicas per workload are stored only when they change, with bounds
  between two polls.
- They are served through `get_availability_history(host, port, range)`.
- They become `availability_outage` / `availability_restored` / `availability_steady` facts.
- The planner reads them when a consumer logged errors about a dependency that looks healthy now.
- Connectivity is not recorded historically.

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
