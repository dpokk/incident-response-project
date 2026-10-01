# INCIDENTS.md

## Purpose

This document is the canonical definition of simulated incidents and investigation scenarios.

The scenarios are used for:
- Live demonstrations
- Regression testing
- Investigator development
- Evidence-model validation
- Future remediation testing

The investigator must not receive the scenario name/type as an input during a real investigation. It must infer the failure from evidence.

---

# Current Iteration-2 Scenarios

## Incident 1 — OOMKilled / Memory Exhaustion

### Intended Failure

A Kubernetes workload exceeds its memory limit and is terminated/restarted.

### Expected Evidence

Potential evidence includes:
- Container/pod state indicating `OOMKilled`
- Kubernetes events
- Previous container logs
- Restart count
- Resource limit
- Memory usage metrics if available

### Expected Diagnosis

The investigator should identify memory exhaustion / OOMKilled as the likely root cause.

### Important Distinction

A frontend or dependent service may be impacted by the backend failure but should not be incorrectly identified as the root cause.

---

## Incident 2 — Database Configuration / Connectivity Failure

### Intended Failure

The application is configured to use an incorrect database target or otherwise cannot connect to the intended PostgreSQL service.

### Expected Evidence

Potential evidence:
- Application logs showing connection/DNS/configuration errors
- Environment/configuration values
- Service information
- Dependency graph
- PostgreSQL service state
- Endpoint/DNS evidence

### Expected Diagnosis

The investigator should distinguish an incorrect database target/configuration problem from PostgreSQL itself being unavailable.

---

## Incident 3 — PostgreSQL Unavailable

### Intended Failure

The intended PostgreSQL service exists but PostgreSQL is unavailable.

### Expected Evidence

Potential evidence:
- PostgreSQL Service exists
- Service has 0 endpoints or unavailable backend
- Backend logs show connection failures
- Dependency graph confirms backend → PostgreSQL dependency
- Kubernetes pod/service state

### Expected Diagnosis

PostgreSQL unavailability should be identified as the likely root cause, with backend as an impacted component.

**How reports represent this (as implemented):**
- **Root-cause component:** `postgres` (the unavailable dependency).
- **Affected component:** `backend` (where the failure surfaces).
- **Failure category:** dependency unavailable.
- **Also impacted:** `frontend` (the caller of backend).

### Important Contrast

This scenario must remain distinguishable from Incident 2.

Example:

```text
Wrong configuration:
Backend → wrong DB target → connection/DNS/configuration failure

PostgreSQL unavailable:
Backend → correct PostgreSQL Service → Service has 0 endpoints / unavailable DB
```

---

## Incident 4 — Application Crash

### Intended Failure

The application process crashes independently of the other infrastructure scenarios.

### Expected Evidence

Potential evidence:
- Container termination state
- Exit code
- Kubernetes restart events
- Current/previous application logs
- Restart count
- Service state

### Expected Diagnosis

The investigator should identify the application crash as the likely root cause rather than incorrectly blaming the service, frontend, or Kubernetes itself.

---

# Primary Future Demonstration Incident

The next major demonstration may use a more causal chain involving traffic and resource saturation.

## Incident 5 — Traffic Spike → Resource Exhaustion → Service Failure

**Status: partially implemented.** Incident 1's live injection (`scripts/inject/oom.ps1`) is exactly this
chain.

What exists today:
- The investigator diagnoses memory exhaustion.
- When Prometheus is available, it reports the traffic increase (e.g. "~100 → ~870 req/s, 8.7x") as a
  **contributing factor**.
- Iteration 1 (archived) reconstructed the full T1–T8 timeline from metrics.
- Iteration 4 reconstructs the incident from facts: onset → development → failure → recovery, with ordering
  and uncertainty. Recovery time is taken from instance readiness, and only when the evidence supports it.
  With retained history, the memory warning of the run that took the spike becomes the onset.

- **Metrics as time-anchored evidence** (Iteration 4).
  - Threshold crossings get *bounded* times: traffic onset, memory at 80% of the limit, error ratio above 5%
    and back below. The bound is widened by a rate's averaging window, because a 15 s rate shows a change up
    to 15 s late.
  - Instances that no longer exist are included.
  - Earlier episodes in the baseline lookback are named, not counted.
  - Memory sampling density is reported as a fact.
- **Correlation, checked by evidence.** Traffic is a contributing factor only when all three hold:
  1. the entry point's configuration shows its traffic reaches the killed component;
  2. the traffic rose before memory evidence on that component;
  3. that memory evidence came before the first kill.

  Otherwise the report lists the correlation as "not linked" or "not established", with the reason. An
  entry point whose configuration was not examined makes the path "unknown", never "absent", and the
  planner examines it when metrics are queried.

CPU saturation is not implemented.

**Live replay of the OOM run of 2026-09-30, 23:21:59–23:27:51:**
- **Traffic:** rose 10.2x, onset between 23:26:44 and 23:27:04. An earlier spike from 23:14 was correctly
  excluded.
- **Kill order:** the first kill was at 23:26:54, inside the traffic interval, so the order was reported as
  undetermined and traffic was **not** claimed as a cause.
- **Memory:** metrics never sampled above 36% of the limit, about one sample every 19 s. This was reported as
  a sampling limit that neither confirms nor contradicts the kills.
- **Diagnosis:** memory exhaustion, 70%. Nothing was retained because the recorder was not running then.

Memory metrics alone cannot show this incident's memory climb: cAdvisor scrapes go sparse under load (13–42
samples per pod in 15 minutes). The application's own memory logs, retained by the recorder, are the
stronger evidence.

### Intended Chain

```text
Traffic spike
    ↓
Request rate increases
    ↓
CPU/memory utilization increases
    ↓
Backend resource exhaustion
    ↓
Pod becomes unhealthy / restarts
    ↓
HTTP 5xx errors increase
```

### Desired Evidence

- Request rate
- HTTP status/error rate
- CPU metrics
- Memory metrics
- Pod state
- Restart count
- Kubernetes events
- Application logs
- Deployment resource limits

### Desired Timeline

The investigator should be able to reconstruct something like:

```text
T1: Request rate begins increasing
T2: CPU/memory utilization increases
T3: Resource threshold exceeded
T4: Pod becomes unhealthy
T5: Kubernetes restarts pod
T6: HTTP 5xx errors increase
T7: Replacement pod becomes ready
T8: Error rate returns toward baseline
```

### Desired Diagnosis

The report should distinguish:

Symptom:
> HTTP 5xx errors increased.

Likely root cause:
> Backend resource exhaustion following a sudden traffic increase.

This scenario is useful because it requires correlation across application behavior and infrastructure state.

---

# Incident Report Requirements

Every incident report should attempt to contain:

1. Incident ID
2. Incident status
3. Affected component
4. Failure category
5. Symptoms
6. Dependencies
7. Evidence
8. Timeline
9. Likely root cause
10. Confidence
11. Impacted components
12. Rejected alternatives
13. Investigation trace
14. Recommendations, where appropriate

## Evidence Rule

The report should distinguish:

### Fact

Example:
> PostgreSQL Service currently has 0 endpoints.

### Observation

Example:
> Backend logs contain repeated PostgreSQL connection failures.

### Diagnosis

Example:
> PostgreSQL unavailability is the likely cause of backend request failures.

Do not represent a model-generated interpretation as an observed infrastructure fact.

---

# Testing Rules

The investigator should be tested against:
- Each failure scenario independently.
- A healthy environment.
- Ambiguous/similar symptoms where possible.
- Evidence that supports the correct root cause.
- Evidence that rules out plausible alternatives.

Scenario identifiers must not be passed to the investigator as hidden hints during the actual investigation path.

The 7 offline tests from the end of Iteration 2 remain the regression baseline inside the larger suite.

Similar-symptom cases now covered (`tests/test_similar_symptoms.py`, 5 cases):

| Case | Looks like | Correct diagnosis |
|---|---|---|
| Crash-loop whose traceback is a refused DB connection | Application crash | Dependency unavailable |
| Exit code 137 from a liveness kill | OOM | Health-check failure |
| DB down right after an unrelated ConfigMap edit | Misconfiguration | Dependency unavailable |
| Readiness noise from a normal rollout | A failure | No incident |
| Backend out of memory and fully down (found in the Iteration 3 final demo) | Dependency unavailable in frontend | Memory exhaustion in backend, with frontend impacted |

Timestamp integrity is covered in `tests/test_timeline.py`. Crash evidence from instances deleted before the
investigation is covered in `tests/test_adapter.py`.

# Future Remediation Test Cases

These are planned, not currently implemented.

Potential remediation examples:

### OOMKilled
Possible plan:
- Increase memory limit.
- Roll out deployment.
- Verify no new OOMKills.
- Verify application health.

### Unhealthy deployment
Possible plan:
- Restart or roll back deployment.
- Verify replicas become healthy.
- Verify error rate returns to baseline.

### Scaling incident
Possible plan:
- Increase replicas.
- Verify CPU/error rate improves.
- Verify expected final state.

All remediation plans must state:
- Current state
- Proposed action
- Expected final state
- Verification criteria
- Risk
- Rollback approach

Execution requires explicit human approval in the planned future system.
