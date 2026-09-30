# PROJECT_VISION.md

## 1. Product Vision

The long-term goal is to build a provider-agnostic incident investigation and remediation platform that can be offered as a service to engineering organizations.

A customer should be able to integrate the platform with its existing production environment and allow its engineers to use it when incidents occur.

The platform should work across heterogeneous environments rather than requiring the customer to replace its infrastructure.

Potential customer environments include:
- Kubernetes
- AWS
- GCP
- Azure
- Databases
- Redis and other data stores
- Monitoring systems
- Logging systems
- CI/CD systems
- Workflow systems
- Application services
- Other infrastructure/platform components

The platform is not intended to be merely an AI chatbot that reads logs. It is intended to provide an incident-response capability.

## 2. Core User Promise

The eventual product should allow an engineering organization to say:

> When something goes wrong in our production environment, the platform investigates what happened, identifies the likely root cause, reconstructs the incident timeline, explains what was affected, communicates the result to engineers, proposes a safe remediation, and — when explicitly authorized — executes and verifies the fix.

## 3. Target End-to-End Workflow

```text
Customer Environment
        ↓
Incident occurs
        ↓
Incident detected / received
        ↓
Build incident context
        ↓
Investigate environment
        ↓
Collect evidence
        ↓
Reconstruct timeline
        ↓
Determine root cause
        ↓
Determine blast radius / impact
        ↓
Generate incident report
        ↓
Slack / Engineer Interface
        ↓
Engineer reviews
        ↓
      ┌──────────────────────┐
      │                      │
      ↓                      ↓
Engineer fixes        Agent proposes
manually              remediation
                              ↓
                       Remediation plan
                              ↓
                       Expected final state
                              ↓
                       Engineer approval
                              ↓
                       Agent executes fix
                              ↓
                       Verify environment
                              ↓
                       Incident resolved
```

## 4. Product Capabilities

### Investigation

The platform should:
- Understand incident context.
- Identify relevant services and dependencies.
- Determine which evidence sources are useful.
- Collect logs, metrics, events, configuration/state, and deployment information.
- Correlate evidence.
- Reconstruct a timeline.
- Distinguish symptoms from root causes.
- Identify impacted components.
- Identify and reject plausible alternatives when evidence supports doing so.
- Produce confidence and evidence-backed conclusions.

### Reporting

The platform should produce an engineer-readable report containing:
- Incident summary
- Severity/status
- Affected components
- Symptoms
- Evidence
- Timeline
- Root cause / likely cause
- Confidence
- Impact/blast radius
- Rejected alternatives
- Investigation trace
- Recommendations

Slack is intended to be a primary engineer-facing communication channel.

### Remediation Planning

The platform should eventually:
- Translate a diagnosed incident into a proposed remediation.
- Describe the current state.
- Describe proposed actions.
- Describe expected final state.
- Identify risks.
- Identify rollback strategy.
- Present the plan to an engineer before execution.

### Remediation Execution

The platform should eventually:
- Require explicit authorization for production-changing actions.
- Enforce customer policies and permissions.
- Execute only approved, typed capabilities.
- Record every action in an audit trail.
- Verify the expected final state.
- Report whether remediation succeeded.
- Return to investigation if verification fails.

## 5. Provider-Agnostic Principle

The investigation engine should reason about capabilities, not vendor-specific commands.

Examples of provider-independent capabilities:

- `get_logs()`
- `get_metrics()`
- `get_service_health()`
- `get_resource_state()`
- `get_events()`
- `get_dependencies()`
- `get_configuration()`
- `get_deployment_history()`

Providers implement these capabilities through adapters/connectors.

Conceptually:

```text
Investigation Engine
        ↓
Capability Interface
        ↓
Provider Adapter
        ↓
Kubernetes / AWS / GCP / Azure / Datadog / etc.
```

Kubernetes is the first environment, not the definition of the product.

Provider agnosticism should be demonstrated empirically by using the same investigation abstractions with at least two meaningfully different provider/infrastructure implementations.

## 6. Service / SaaS Model

The eventual product should be a service rather than a codebase customers have to fork.

The intended architecture has:

### Our Control Plane
- Tenant/organization management
- Incident management
- Investigation orchestration
- Evidence/timeline processing
- Root-cause analysis
- Remediation planning
- Policy enforcement
- Audit logs
- Slack/UI/API
- Model/runtime orchestration

### Customer-Side Connector / Execution Plane
A lightweight connector runs in or near the customer's environment and provides controlled access to permitted systems.

Initially it should be read-only.

Later it can expose narrowly defined write capabilities for approved remediation.

The connector should prevent the core platform from requiring unrestricted access to customer infrastructure.

## 7. Engineer Experience

The desired engineer workflow is centered around Slack and a web interface as needed.

Example:

```text
🔴 Production Incident

Service: checkout-api

Root Cause:
Backend pods became resource constrained following a traffic increase.

Impact:
32% of requests affected.

Timeline:
14:02 Traffic increased
14:03 CPU saturation
14:04 Pods restarted
14:04 HTTP 5xx increased

[View Investigation]
[Create Remediation Plan]
```

For remediation:

```text
Proposed Remediation

Current state:
Replicas: 4
Memory limit: 1 GiB

Actions:
1. Increase memory limit to 2 GiB.
2. Increase replicas to 8.
3. Perform rolling deployment.
4. Verify error rate and pod health.

Expected final state:
Error rate < 1%
All replicas healthy
No OOMKills

[Execute]
[I'll Fix It Myself]
```

The agent should never be assumed to have permission to make production changes merely because it generated a plan.

## 8. Safety Model

The long-term product should have explicit boundaries:

### Read
Generally safe, subject to customer permissions.

### Plan
The agent can reason about possible remediation without changing the environment.

### Execute
Production-changing actions require explicit authorization and policy checks.

Actions should eventually be typed/constrained rather than arbitrary shell execution.

Example:

```text
scale_deployment(service, replicas)
restart_deployment(service)
rollback_deployment(service, revision)
```

rather than unrestricted:

```text
run_arbitrary_shell(command)
```

Every execution should be auditable.

## 9. Product Evolution

The intended evolution is:

```text
Kubernetes incident investigation
        ↓
Generalized evidence-driven investigation
        ↓
Capability-based / AI-assisted investigation
        ↓
Slack-centered incident workflow
        ↓
Remediation planning
        ↓
Human-approved remediation
        ↓
Remediation verification
        ↓
Provider abstraction + second provider
        ↓
SaaS control plane + customer connector
        ↓
External pilot
```
