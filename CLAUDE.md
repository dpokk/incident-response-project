# CLAUDE.md — Project Instructions and Persistent Context

## Purpose

This repository contains a research/prototype project that is evolving toward a provider-agnostic production incident investigation and remediation platform.

Claude Code MUST read and use these files as persistent project context:

- `docs/PROJECT_VISION.md` — long-term product vision and target user workflow.
- `docs/ROADMAP.md` — current project status, completed iterations, next milestones, and sequencing.
- `docs/ARCHITECTURE.md` — architectural principles and decisions that should guide implementation.
- `docs/INCIDENTS.md` — simulated incidents, evidence expectations, and test scenarios.

These documents are the source of truth for project direction. Before making substantial architectural or implementation changes, consult the relevant documents.

## Current Project State

Iterations 1, 2 and 3 have been completed. The next milestone is Iteration 4 (richer observability + Slack
workflow).

### Iteration 1
The original prototype detected a Kubernetes `OOMKilled` incident using a Python-based investigator.

It also included:
- a Prometheus-based T1–T8 timeline;
- quantified impact;
- an LLM-written narrative with recommendations;
- Slack posting.

Code is archived in `archive/iteration1/`. Iteration 2 intentionally dropped the LLM narrative,
recommendations, quantified impact and the metric-derived timeline; they have not been restored.

### Iteration 2
The system was generalized into an evidence-driven investigation pipeline capable of diagnosing four failure scenarios without being told which scenario was intentionally injected:

1. OOMKilled / memory exhaustion
2. Database configuration/connectivity failure
3. PostgreSQL unavailable
4. Application crash

The current investigator can collect and reason over:
- Pod/container state
- Kubernetes events
- Current and previous logs
- Services and endpoints
- Configuration
- Dependencies
- Deployment/ConfigMap changes
- Optional metrics

It produces structured reports containing incident ID, affected component, failure category, dependencies, symptoms, evidence, diagnosis, likely root cause, confidence, rejected alternatives, timeline, and investigation trace.

A fake Kubernetes environment exists for offline testing. At the end of Iteration 2, 7/7 offline tests
passed and all four scenarios were tested live. Those tests remain the regression baseline within the
current suite.

### Repository layout (actual)

| Path | Contents |
|---|---|
| `app/` | Demo system: `frontend.py`, `backend.py` (PostgreSQL + invoice job), `loadgen.py`/`loadgenctl.py`, Dockerfile |
| `k8s/` | Manifests: namespaces, PostgreSQL, shop (frontend/backend), load generator, Prometheus |
| `scripts/` | `setup.ps1`, `inject/*.ps1` (one per failure scenario), `restore.ps1` (manual recovery) |
| `investigator/` | The investigator (see `README.md` for the layer table) |
| `investigator/capabilities/` | Capability interface + Kubernetes / Prometheus adapters |
| `tests/` | Offline tests against `tests/fake_cluster.py` (a fake Kubernetes API) |
| `archive/iteration1/` | Iteration 1 investigator, kept for reference |
| `docs/` | Vision, roadmap, architecture, incidents |

`reports/` and `state/` are generated at runtime and are not in git. There is no `src/` directory.

### Iteration 3 — completed (merged to `main`, together with the stabilization pass)

Done:
- **Capability layer** (`investigator/capabilities/`):
  - `ResourceProvider` / `MetricsProvider` interfaces with neutral records;
  - `KubernetesAdapter` and `PrometheusMetrics` as the adapters;
  - a traced, cached `Capabilities` facade.
- **Incident context and planner** (`context.py`, `planner.py`): deterministic, evidence-driven, and
  every decision recorded with its reason.
- **Detection on capabilities** (`detector.py`), with `providers.py` as the composition root.
- **Provider-neutral fact vocabulary and semantics:** termination causes, event categories and waiting
  causes are mapped in the adapter, and `legacy.py` reads old evidence.

Architecture tests keep provider code and vocabulary out of the reasoning modules. Kubernetes is the only
resource provider, so provider agnosticism is not claimed.

### Important Current Boundary

The investigator deliberately stops at diagnosis (unchanged in Iteration 3).

There is currently:
- No automated remediation
- No autonomous production actions
- No LLM/AI investigator
- No provider-agnostic multi-cloud implementation yet
- No SaaS/multi-tenant control plane yet
- No customer connector yet

Do not assume these capabilities already exist.

## Immediate Direction

The next major step is NOT to add many more Kubernetes failure scenarios.

The next step is to evolve the existing deterministic investigator into a more general, capability-based investigation system.

The intended progression is:

Current deterministic investigator
→ capability abstraction
→ adaptive/AI-assisted investigation
→ richer observability and Slack workflow
→ remediation planning
→ human-approved remediation
→ verification
→ second provider
→ SaaS control plane + customer connector
→ external pilot

## Engineering Principles

1. Preserve working functionality. Do not rewrite the existing investigator without a concrete reason.
2. Prefer incremental refactoring over a large rewrite.
3. Separate provider-specific mechanisms from provider-independent investigation logic.
4. Represent infrastructure operations as explicit capabilities/interfaces rather than embedding provider-specific commands throughout the reasoning layer.
5. Evidence must remain traceable. Diagnoses should be linked to facts/evidence whenever practical.
6. Distinguish symptoms, evidence, diagnosis, root cause, impacted components, and alternatives.
7. Do not make the LLM the sole source of truth for factual infrastructure state.
8. Do not introduce autonomous remediation before the investigation and remediation-plan workflows are reliable.
9. Production-changing actions must eventually cross an explicit authorization boundary.
10. Any remediation system must verify the resulting system state rather than assuming command success means incident resolution.
11. Keep the prototype simple enough to demonstrate locally, but avoid design choices that make future provider abstraction impossible.
12. Do not claim provider agnosticism until at least two meaningfully different provider/infrastructure implementations have exercised the same investigation abstractions.

## How Claude Should Work

Before substantial work:
- Read `CLAUDE.md`.
- Read the relevant docs under `docs/`.
- Inspect the existing implementation before proposing replacement architecture.
- Identify what is already implemented versus what is only planned.
- Preserve the existing iteration-2 capabilities unless the task explicitly changes them.

When implementing:
- Explain important architectural changes.
- Add or update tests for investigation behavior.
- Keep simulated incidents reproducible.
- Prefer deterministic evidence collection and validation around model-assisted reasoning.
- Keep provider-specific code behind explicit adapters/capabilities where practical.

When discussing future functionality:
- Clearly label it as planned/not implemented.
- Never describe roadmap items as existing capabilities.

## Current Demonstration Goal

The near-term demonstration is a local Kubernetes incident investigation flow:

incident occurs
→ incident detected
→ context created
→ relevant evidence collected
→ evidence correlated
→ timeline reconstructed
→ likely root cause identified
→ impact/blast radius determined
→ structured report generated
→ report posted to Slack

The initial remediation workflow is a later milestone and should not be silently introduced into the current diagnosis implementation.

## Source of Truth Rule

If there is a conflict:
1. Actual working code/tests determine what is implemented.
2. `docs/ROADMAP.md` determines intended project sequencing.
3. `docs/PROJECT_VISION.md` determines long-term goals.
4. `docs/ARCHITECTURE.md` determines architectural principles/decisions.
5. `docs/INCIDENTS.md` determines the canonical simulated incident scenarios.

If implementation and documentation disagree, do not silently assume the docs are correct. Call out the discrepancy and update the appropriate documentation when the intended direction is confirmed.
