# ROADMAP.md

## Current Status

### Iteration 1 — COMPLETED
**Goal:** Detect a Kubernetes OOMKilled incident.

The original prototype used a Python-based investigator to identify an `OOMKilled` failure.

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
- 7/7 offline tests pass.
- All four failure scenarios tested live.

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

### Success Criteria

Iteration 3 should demonstrate that:
1. The investigator can request evidence through capabilities rather than directly depending on Kubernetes implementation details.
2. Existing four scenarios continue to work.
3. Existing offline tests remain green or are updated appropriately.
4. Evidence remains traceable.
5. The system can decide which evidence/capabilities are relevant to an incident.
6. The final diagnosis remains evidence-driven.

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
