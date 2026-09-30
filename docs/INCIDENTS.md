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

This is a planned future scenario and should not be treated as already implemented unless the code proves it.

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

The existing offline test suite has 7/7 passing tests and should remain a regression baseline.

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
