# Project results: measurements, test results and outcomes

This document collects every measured result of the project in one place, for the major-project report. Each
section says what was measured, how, the result, and what it shows. Numbers come from the offline test suite, from
benchmark scripts run against the simulated cluster, and from live runs against the real local Kubernetes cluster.
Earlier live records are in `docs/validation/`; this document summarises them and adds new measurements taken on
2026-11-10 and 2026-11-11.

Where something was not measured, or was measured only once, that is said. A single live run is a demonstration,
not a statistic.

## Contents

1. [The system in brief](#1-the-system-in-brief)
2. [What was built](#2-what-was-built)
3. [Engineering metrics](#3-engineering-metrics)
4. [Offline diagnosis accuracy](#4-offline-diagnosis-accuracy)
5. [Historical evidence: investigating after the evidence is gone](#5-historical-evidence-investigating-after-the-evidence-is-gone)
6. [Human review in Slack](#6-human-review-in-slack)
7. [Approved remediation and verification](#7-approved-remediation-and-verification)
8. [Load calibration experiment](#8-load-calibration-experiment)
9. [End-to-end runs of every incident](#9-end-to-end-runs-of-every-incident)
10. [The multi-agent design: Investigator, Reviewer, Remediation](#10-the-multi-agent-design-investigator-reviewer-remediation)
11. [False alarms and resilience](#11-false-alarms-and-resilience)
12. [Safety properties](#12-safety-properties)
13. [Problems found and fixed through validation](#13-problems-found-and-fixed-through-validation)
14. [Known limitations](#14-known-limitations)
15. [Headline results](#15-headline-results)
16. [How to reproduce](#16-how-to-reproduce)

---

## 1. The system in brief

An incident response platform for a service running on Kubernetes. It detects a failure, investigates it from
evidence, reconstructs what happened, proposes a typed fix, gets a human decision in Slack, applies exactly one
checked change, and verifies the result.

**Environment used for every live result:**
- a local Kubernetes cluster (minikube, profile `incident-demo`) running a demo shop: frontend, backend and
  PostgreSQL, with a load generator sending about 100 requests per second;
- Prometheus for traffic and error metrics;
- Slack (workspace "MajorProject") for the incident messages and the Approve and Execute buttons;
- NVIDIA NIM for the two LLM agents: `nemotron-3-ultra-550b-a55b` (Investigator) and
  `nemotron-3.5-super-vl-120b-a12b` (Reviewer);
- a web console (FastAPI, Server-Sent Events) that shows every real step live.

**The incident flow:**

```
detect → collect evidence (rule engine) → diagnose → Investigator Agent (LLM) → Reviewer Agent (LLM)
  → typed remediation plan → policy gate
      gate passes:  Remediation Agent applies it (no click) → verify
      otherwise:    engineer approves, then executes, in Slack → policy, recheck, dry run, one change → verify
```

## 2. What was built

The project was built in iterations. Each one was completed, tested and, where it touches the live system,
validated against the cluster.

| Iteration | What it added | Validation record |
|---|---|---|
| 1 | Detect an OOMKilled incident; Prometheus timeline; LLM narrative; Slack posting (archived) | `archive/iteration1/` |
| 2 | Evidence-driven investigation of four failure types, without being told which one was injected | 7/7 offline tests; 4 scenarios live |
| 3 | Capability layer (provider-neutral interfaces, Kubernetes and Prometheus adapters); evidence-driven planner | architecture tests |
| 4 | Evidence history (a local recorder) and incident reconstruction: timeline, impact | `iter-04-oom-postmortem.md` |
| 5 | Remediation planning: typed, evidence-backed plans that execute nothing | offline tests |
| 6 | Slack incident thread and human review, decisions bound to the exact plan | `iter-06-slack-review.md` |
| 7 | Approved execution: policy, live recheck, dry run, one typed change, verification, rollback | `iter-07-live-demos.md`, `iter-07-demo-a-calibration.md` |
| Demo layer | Web console; Investigator Agent (LLM); deterministic Remediation Agent with a policy gate | `DEMO.md` |
| Multi-agent | Reviewer Agent: a second LLM agent that checks the Investigator and can only block | `demo-reviewer-agent-live.md` |
| Reliability | Short blips no longer open incidents; agents get timing facts; reconnect after a cluster restart; model fallback | this document, section 11 |

**Failure scenarios the system handles** (each is a real injection into Kubernetes, `scripts/inject/`):

| Scenario | What is injected |
|---|---|
| Database down | PostgreSQL scaled to 0 |
| Wrong database host | `DATABASE_URL` points at a host that does not exist |
| Memory exhaustion (spike) | a 90 s traffic spike to 800 req/s; the backend is OOM-killed |
| Memory limit set too low | a deploy sets the backend's limit to 32Mi; pods crash-loop |
| Application crash | one bad order makes the backend divide by zero; pods crash-loop |
| Broken release | the backend points at an image that does not exist |
| Backend scaled to zero | the frontend has nothing to call |
| Stale database credentials | the backend reads a rotated, stale password |
| Sustained overload | 800 req/s for 15 min; more memory is not a fix |

**Remediation actions** (typed; no commands, no YAML): adjust a memory limit, scale a workload, restore one
configuration value (with a restart), roll back a release to its previous image.

## 3. Engineering metrics

Measured on 2026-11-11 on `main` (commit `7ee0d01`).

### Code size

| Area | Files | Lines |
|---|---|---|
| `investigator/` (detection, evidence, diagnosis, timeline, impact, planning, review, execution, Slack) | 42 | 9,120 |
| of which `capabilities/` (provider interfaces and adapters) | 6 | 1,226 |
| of which `actuators/` (the only cluster writer) | 3 | 206 |
| `demo/` (engine, console, agents, Slack messages) | 16 | 3,698 |
| of which `demo/agent/` (LLM agents and their read-only tools) | 5 | 780 |
| `tests/` | 30 | 5,024 |
| `app/` (the demo shop) | 5 | 777 |
| `k8s/` manifests and `scripts/` | 19 | 539 |
| Documentation (Markdown) | | 3,596 |

### Automated tests

**273 tests, all passing.** The suite runs offline against a fake Kubernetes API (`tests/fake_cluster.py`) and
scripted LLM models, with no network and no cluster. Measured run time: 17.8 s, 17.6 s and 16.8 s over three runs.

At the end of Iteration 2 the suite had 7 tests; they remain in the suite as the regression baseline.

| Area | Tests |
|---|---|
| Human review and Slack (`test_slack_review`, `test_slack_execution`, `test_demo_mirror`) | 42 |
| Execution, verification and rollback (`test_execution*`, `test_verification`, `test_rollback_release`) | 55 |
| Remediation planning and the Remediation Agent (`test_remediation`, `test_demo_remediation_agent`, `test_demo_agent_plan`) | 30 |
| Evidence history, timeline, impact and metrics (`test_history`, `test_recorder`, `test_timeline*`, `test_impact`, `test_metrics_correlation`, `test_temporal_semantics`, `test_dependency_history`) | 76 |
| Diagnosis scenarios, planner and similar-symptom traps (`test_scenarios`, `test_planner`, `test_similar_symptoms`, `test_legacy`, `test_adapter`) | 25 |
| Detection (`test_detector`) | 10 |
| LLM agents, timing facts and reconnecting (`test_demo_agent`, `test_demo_reviewer`, `test_demo_hybrid`, `test_demo_timing`) | 24 |
| Architecture boundaries (`test_architecture`) | 11 |

The 11 architecture tests check, by reading the code, that the design rules hold. For example: only the actuator
adapter writes to the cluster; only the executor holds an actuator; the actuator interface is exactly four typed
operations; planning, review and Slack have no path to execute; the reasoning modules use provider-neutral
vocabulary.

### History

76 commits from 2026-09-30 to 2026-11-11.

## 4. Offline diagnosis accuracy

**What was measured.** The deterministic rule engine was run on every simulated failure in the fake cluster, with
both of its evidence-collection strategies:
- *planned:* it decides what to collect next from what it has found;
- *exhaustive:* it collects everything.

It is given only the symptoms, never the scenario. Script: `scripts/measure/offline_bench.py`.

| Scenario | What makes it hard | Diagnosis | Root-cause component | Confidence |
|---|---|---|---|---|
| Healthy cluster | nothing is wrong | Undetermined (correct) | none | 0% |
| OOM kill | | Memory exhaustion | backend | 85% |
| Wrong database host | the database itself is healthy | Dependency misconfiguration | `DATABASE_URL` in the backend's ConfigMap | 97% |
| Database down | the backend shows the errors | Dependency unavailable | postgres | 97% |
| Application crash | | Application crash | backend | 90% |
| Crash caused by the database being down | the crash is a symptom, not the cause | Dependency unavailable | postgres | 71% |
| Liveness-probe kill | exit code 137, like an OOM kill | Health-check failure (not memory) | backend | 55% |
| Database down after an unrelated config edit | a recent change that is not the cause | Dependency unavailable | postgres | 97% |
| Normal rollout, briefly not ready | looks like a failure, is not one | Undetermined (correct) | none | 0% |
| OOM with the backend completely down | the frontend's "backend unavailable" errors dominate | Memory exhaustion | backend | 85% |

**Result: 20 of 20 correct** (10 scenarios × 2 strategies). Both strategies reached the same diagnosis and the
same confidence in every case.

**Efficiency of the planner:**

| Strategy | Capability calls per investigation | Facts collected |
|---|---|---|
| Planned | 22–26 | 21–35 |
| Exhaustive | 28–31 | 23–35 |

The planner reached the same answers with about 20% fewer calls. Each investigation took 1–4 ms on the fake cluster.
Live, collection takes seconds because each call goes to the Kubernetes API.

**What it shows.** The diagnosis is evidence-driven, not scenario-driven. Four of the ten scenarios are deliberate
traps (a symptom that looks like a cause, an exit code that looks like an OOM, a recent change that is not the cause,
a healthy rollout), and the engine was not fooled. Confidence is lower where the evidence is genuinely ambiguous
(55–71%).

## 5. Historical evidence: investigating after the evidence is gone

From `docs/validation/iter-04-oom-postmortem.md` (live, 2026-10-01). The same investigation was run twice on the
same time window: with the evidence history the recorder keeps, and without it.

| Experiment | Without history | With history |
|---|---|---|
| OOM, then the killed pods were replaced (their logs gone) | **Undetermined, 0%** | **Memory exhaustion, 85%** |
| OOM crash-loop | Memory exhaustion, 70% (2 kills seen) | Memory exhaustion, 85% (all 8 kills and the memory warning) |
| PostgreSQL down, recovered before investigating | **Application crash, 75% (wrong)** | **Dependency unavailable (correct)**, 57%, with a timeline and impact |

**What it shows.** Kubernetes forgets quickly: deleted pods take their logs with them. Recording evidence while the
system runs turns an unexplained incident into a diagnosed one, and corrects a wrong diagnosis. The engine is the
same in both runs; only the evidence differs.

With history, the report also reconstructed the timeline (onset, first kill, recovery) and the impact. In the first
experiment that was about 4,070 of 5,702 requests failed over about 31 s, stated as an estimate from the sampled
rate and error ratio.

## 6. Human review in Slack

From `docs/validation/iter-06-slack-review.md` (live, 2026-10-01).

**What was tested.** An OOM incident, posted to Slack, reviewed by the configured approver.

| Attempt | Result |
|---|---|
| Approve without a value | Refused: parameter missing |
| Approve with `128Mi` (lower than the current 192Mi) | Refused: must be higher than the current value |
| Approve with `256Mi` | Recorded, effective |
| Acknowledge the "investigate further" action | Recorded, effective |

**Verified afterwards:**
- every attempt was audited, bound to the plan's digest;
- the digest recomputed from the stored plan matched;
- **nothing in the cluster changed**: every Deployment, ConfigMap, Service and Secret kept its generation and spec;
- every Slack API call succeeded.

**What it shows.** A decision is recorded, validated and audited; it never changes the system by itself. Values are
typed by the engineer and validated, never inferred.

## 7. Approved remediation and verification

From `docs/validation/iter-07-live-demos.md` (live, 2026-10-01). Each execution went through: authorisation,
approval on the exact plan, plan age, policy, a one-time claim, a live recheck, a dry run, exactly one typed change,
and verification over a settle period and a 120 s observation window.

| Demo | Change | Outcome |
|---|---|---|
| PostgreSQL scaled to 0 | scale postgres 0 → 1 | **RESOLVED**: settled in 26 s; all 5 criteria held on every sample |
| Wrong database host, first run | restore the recorded previous host | **NOT_RESOLVED**: the fix worked, but an unrelated stall pushed errors to 15.1% during the window |
| Wrong database host, re-run | same | **RESOLVED**: settled in 21 s; all 4 criteria held |
| Deliberately insufficient fix (overload) | memory 192Mi → 256Mi | **NOT_RESOLVED** as predicted: 8 new OOM kills after the change |
| Rollback of that fix | memory 256Mi → 192Mi (recorded previous value) | **RESOLVED**: backend ready on 25/25 samples |

**Verified in every case:** only the intended object changed (checked from Kubernetes generations and
resourceVersions); the audit chain matched the approved plan digest; a failure made no further change and offered a
typed rollback for its own approval.

**What it shows.** "The command succeeded" is never taken as "the incident is resolved": the verifier judges the
observed state, and reports a failed or doubtful fix honestly instead of calling it success.

## 8. Load calibration experiment

From `docs/validation/iter-07-demo-a-calibration.md` (live, 2026-10-01). The question: is there a load at which the
backend keeps being OOM-killed at 192Mi but stays healthy at 512Mi *because of* the larger limit? Eleven runs:

| Finding | Evidence |
|---|---|
| The backend's capacity is about 190 req/s for 2 replicas | stable at 130, 170, 185; backlog grows at 200 |
| Above capacity, a larger limit only delays the kill | 6 kills at 200 req/s; backlog growth ~25/s per pod |
| After an OOM cascade, 170 req/s keeps 192Mi failing | 7 kills, 95.1% user failures in 4 min |
| Any rollout ended the cascade, even an insufficient 200Mi | 200Mi: one more kill, then stable |
| At 170 req/s, 512Mi absorbed a 203 MB peak with no kill | 200Mi was killed once |

**Conclusion.** No load was found where the larger limit itself is the fix. The planned "OOM fixed by a higher
limit" demo would have attributed a recovery to the wrong cause, so it was not run as planned. This is reported as a
documented exception in Iteration 7.

**What it shows.** Results were checked for the right cause, not only the right outcome. The finding was later
confirmed live: on Sustained overload the Reviewer Agent challenged a memory fix, and executing that fix came back
NOT_RESOLVED (section 10).

## 9. End-to-end runs of every incident

**What was measured.** Every incident was run end to end on the current code (`main`, commit `7ee0d01`), on
2026-11-11, with automatic remediation on. A script triggered each incident from the console's API, followed it to its
decision point, and read every step from the console's event stream. For incidents that need an engineer, the run
stopped at the approval request and was reset: the engineer's own reaction time is not a property of the system. The
engineer-approved outcomes for those incidents are in section 10 (2026-11-10).

### 9.1 Detection latency

Time from triggering the injection to the incident being opened. Each run started after the system had been healthy
for at least 75 s, past the 60 s detection pause that follows a reset.

| Incident | Injection script finished | Incident opened | What the time includes |
|---|---|---|---|
| Database down | 1 s | **7 s** | the synthetic probe failing for two polls |
| Broken release | 0.4 s | **10 s** | the new pod failing to pull its image |
| Application crash | 0.7 s | **18 s** | the backend's job reading the bad order and crashing |
| Memory limit set too low | 0.4 s | **29 s** | new pods starting with 32Mi, then being OOM-killed |
| Backend scaled to zero | 0.4 s | **30 s** | the backend pods shutting down gracefully |
| Wrong database host | 19 s | **30 s** | the script's own rollout of the backend (11 s after it finished) |

**Result: every incident was detected within 7–30 s of the injection**, most of that being the failure itself taking
effect. Database down was also measured on 2026-11-10 at 6 s and 7 s.

### 9.2 Investigation, review and outcome

From detection, the console waits 20 s to let symptoms accumulate, then the rule engine collects evidence (2–9 s),
then the two agents run.

| Incident | Rule engine | Route | Investigator Agent: fix · findings supported · tool calls · time | Reviewer Agent: verdict · calls · time | Detection → decision ready | Outcome |
|---|---|---|---|---|---|---|
| Database down | Dependency unavailable, 97% | verify | scale postgres to 1 · 6/6 · 5 · 31 s | agree · 4 · 7 s | 68 s | **Resolved automatically** |
| Wrong database host | Dependency misconfiguration, 97% | verify | restore `DATABASE_URL` host · 6/6 · 4 · 31 s | agree · 3 · 10 s | 75 s | Waits for an engineer (config restores always do) |
| Memory limit set too low | Memory exhaustion, 85% | verify | memory limit back to 192Mi · 4/4 · 5 · 46 s | agree · 4 · 36 s | 119 s | **Resolved automatically** (192Mi, first attempt) |
| Application crash | Application crash, 90% | verify | investigate only · 4/4 · 4 · 28 s | agree · 4 · 11 s | 71 s | Handed to an engineer with manual steps |
| Broken release | Image unavailable, 63% | lead | roll back to `app:0.2` · 5/5 · 9 · 46 s | agree · 3 · 13 s | 87 s | Waits for an engineer (rollbacks always do) |
| Backend scaled to zero | Undetermined, 0% | lead | scale backend to 2 · 8/8 · 14 · 56 s | agree · 4 · 22 s | 114 s | Waits for an engineer |
| Stale database credentials | Dependency misconfiguration, 70% | lead | restore the credentials Secret · 8/8 · 14 · 94 s | agree · 4 · 36 s | 159 s | Handed to an engineer with manual steps |
| Memory exhaustion (spike) | Memory exhaustion, 70% | lead | memory to 512Mi · 7/7 · 14 · 159 s | **challenge** · 3 · 30 s | 224 s | **Automation blocked by the Reviewer**; waits for an engineer |
| Sustained overload | Memory exhaustion, 70% | lead | memory to 512Mi · 7/7 · 13 · 132 s | agree · 4 · 55 s | 220 s | Automation refused (agent-led); waits for an engineer |

**Results:**
- **Every incident reached the right kind of outcome:** fixed automatically where the policy allows, an
  approval request where it needs an engineer, manual steps where no typed fix exists.
- **55 of 55 Investigator findings were supported by collected evidence.** No invalid citation was made.
- **Decision ready 68–224 s after detection** (including the 20 s symptom collection). Investigator: 28–46 s when
  verifying the rule engine, 46–159 s when leading. Reviewer: 7–55 s, median 22 s.
- **Automatic fixes:** 2 of 2 eligible incidents were resolved on the first attempt, about 160–205 s from detection
  to verified recovery, most of which is the verification window.
- **No model failures** (the fallback was not needed) and **no Slack failures**.

**The Reviewer's two challenges, both valid:**
- *Memory exhaustion.* "Measured memory peaks at about 58 MB, far below the 192Mi limit … the 512Mi value appears
  arbitrary." Three objections, all citing evidence. Section 8's calibration and the overload result in section 10
  support the same caution.
- *Broken release* (a second run of five incidents, used for 9.1). "The fix description names
  `incident-demo/app:0.3` as the previous working image, but … 0.3 is the image that fails to pull." The
  Investigator's text contradicted its own fix. The rollback value itself was correct, and rollbacks need an
  engineer anyway, but the challenge put the contradiction in front of the person approving it.

**Variation between runs.** The LLM verdicts are not identical from run to run. Sustained overload was challenged on
2026-11-10 and agreed on 2026-11-11, where the gate refused automation anyway (the agent led). Memory exhaustion and
Broken release were agreed on 2026-11-10 and challenged on 2026-11-11. Over the three live passes there were 23
reviews: 20 agreements and 3 challenges, every challenge backed by valid evidence, and no rejects or unsupported
objections. Because the Reviewer can only block, this variation can make the system more cautious, never less.

**Compared with the day before.** The Investigator was faster when leading (46–159 s, against 140–285 s on
2026-11-10), and it completed every run; on 2026-11-10 it had timed out twice.

## 10. The multi-agent design: Investigator, Reviewer, Remediation

### The three agents

| Agent | Kind | Role | Can change the system? |
|---|---|---|---|
| Investigator Agent | LLM, `nemotron-3-ultra-550b-a55b` | Investigates with 14 read-only tools; verifies the rule engine when it is confident, leads when it is not | No |
| Reviewer Agent | LLM, `nemotron-3.5-super-vl-120b-a12b` | Checks the Investigator's conclusion and fix with the same tools; agree, challenge or reject | No; it can only block automation |
| Remediation Agent | Deterministic | Applies a fix without a click, only if every policy gate check passes; tries a bounded ladder of values; reverts if nothing works | Only through the same executor and checks as a human |

Every claim an LLM agent makes must cite evidence ids that exist; a finding or objection that cites made-up
evidence is marked unsupported. If a model fails, the deterministic findings stand.

### Choosing the Reviewer's model (2026-11-10)

Every candidate model on the NVIDIA catalogue was given the same two-step tool-calling review task with this project's
key.

| Model | Result |
|---|---|
| `nemotron-3.5-super-vl-120b-a12b` | **Completed in 6.9 s**, correct tool calls and citations |
| `nemotron-3-ultra-550b-a55b` | Completed in 21.1 s |
| `deepseek-v4.1-flash`, `glm-5.3-flash`, `minimax-m3.1-flash`, `gemma-4-31b-it` | Timed out (tried in parallel and one at a time) |
| `muse-glimmer-30b` | 84 s for one call, then timed out |
| `kimi-k2.6` | Not available for this account (404) |

Before the live runs, the Reviewer was also given a correct diagnosis and a planted wrong one (frontend memory, when
PostgreSQL was at 0 replicas). It **agreed** with the correct one (3 tool calls, 14.5 s) and **rejected** the wrong
one with two objections, both citing real evidence (4 tool calls, 18.2 s).

### Live run of every incident with the Reviewer (2026-11-10)

From `docs/validation/demo-reviewer-agent-live.md`. All nine incidents, with Slack approvals made by the engineer.

| Incident | Rule engine | Route | Reviewer | Result |
|---|---|---|---|---|
| Database down | high | verify | agree (3 calls, 26 s) | **Resolved automatically** |
| Wrong database host | 97% | verify | agree (5 calls, 27 s) | Approved in Slack, RESOLVED |
| Memory exhaustion | 70% | lead | agree (3 calls, 49 s) | Approved in Slack, RESOLVED |
| Stale credentials | 70% | lead | agree (4 calls, 74 s) | Handed to an engineer (no typed fix) |
| Backend scaled to zero | 0% | lead | agree (4 calls, 60 s) | Approved in Slack, RESOLVED |
| Memory limit too low | 85% | verify | agree (4 calls, 56 s) | **Resolved automatically** (512Mi, attempt 1) |
| Application crash | 90% | verify | agree (4 calls, 13 s) | Handed to an engineer (no typed fix) |
| Broken release | 90% | verify | agree (4 calls, 36 s) | Approved in Slack, RESOLVED |
| **Sustained overload** | 85% | verify | **challenge** (4 calls, 74 s) | Automation blocked; executed by hand: **NOT_RESOLVED** |

**The key result.** On Sustained overload every other gate check passed, so without the Reviewer the Remediation
Agent would have raised the memory limit by itself. The Reviewer challenged with three objections, all citing real
evidence: peak memory about 40 MiB against a 192 MiB limit, and the logs saying "workers cannot keep up". When the fix
was executed by hand anyway, verification returned NOT_RESOLVED. The Reviewer was right, and it was the only check
that stopped the wrong automatic fix.

When it agreed, the flow was exactly as without it. Its cost: 13–74 s per incident.

### Where the agents are weak

On a 3-second blip that had already recovered (22:39 on 2026-11-10), the Investigator blamed configuration changes
from earlier test runs and hour-old restarts, and the Reviewer caught only part of that. Agents are reliable on
persistent, single-cause failures and weak on short transient ones with noisy history. Nothing was changed in that
case: no fix was executable and the challenge blocked automation. Section 11 describes the fixes that followed.

## 11. False alarms and resilience

### Before the fixes

On 2026-11-10, about three hours of running opened **two false incidents** from short stalls (22:28 and 22:39). A
cluster restart opened a third, because every container "restarted". The NVIDIA endpoint timed out on the
Investigator twice, leaving no report.

### Fixes (2026-11-11)

- **Detection:** each error log line is counted once; error-log bursts and the 5xx ratio, which lag the system, must
  hold for 30 s on their own before opening an incident. With the synthetic probe failing, two polls are enough as
  before.
- **Timing facts:** both agents are told the incident window and whether the symptoms are still present; every
  restart and change in their tool results says when it happened and whether it is inside the window.
- **Reconnect:** after a cluster restart, the monitor and the log reader read the kubeconfig again; the Prometheus
  port-forward restarts itself; detection starts afresh after 90 s.
- **Fallback:** if the Investigator's model stops answering, the same investigation continues with the Reviewer's
  model.

### After the fixes: measured

| Test | Result |
|---|---|
| **10 minutes idle**, nothing injected (297 samples) | **0 incidents**, 0 unhealthy samples |
| **5 stalls of 3 s** (both backend pods frozen) | Symptoms seen every time (5xx up to 12.6%, up to 172 error lines); **0 incidents** |
| **2 stalls of 6 s** | Symptoms seen (5xx up to 45%, up to 637 error lines); **0 incidents** |
| Database down after the fix | Detected **6 s** after the injection; fixed automatically |
| Prometheus port-forward killed | Restarted by itself; **no gap** in the traffic readings |
| Cluster restarted under the running demo (API port changed) | Recovered without restarting the demo; **no false incident** |

**What it shows.** The false-alarm rate on this setup went from two false incidents in about three hours to none in
the measured runs, without slowing the detection of real failures.

## 12. Safety properties

Each property is enforced in code and tested; most were also observed live.

| Property | How it is enforced | Evidence |
|---|---|---|
| Nothing changes without a human approval and a separate Execute, or a passed policy gate | review and execution are separate steps; the gate's checks are all required | live: iterations 6 and 7; tests |
| Only typed changes; no commands, shell, kubectl or YAML | the actuator interface has exactly four typed operations | architecture test |
| Exactly one change per execution, never twice | an atomic, permanent claim | test; live: one `applied` line per execution |
| The plan executed is the plan approved | decisions are bound to the plan's digest | live: digests matched |
| The system is still as the plan expected | a live recheck before the change | live: "still 192Mi, as the plan expected" |
| The cluster accepts the change | a dry run first | live, every execution |
| Success is verified, not assumed | settle period plus observation window against the plan's own criteria | live: RESOLVED and NOT_RESOLVED both reported |
| A failed fix is not retried or chained | the executor stops; a rollback is a new plan with its own approval | live: Demo D |
| LLM agents cannot change anything | their tools are read-only; the Reviewer can only block | tests; tool list |
| LLM claims are checked | every cited evidence id must exist | tests; live "unsupported" handling |
| Secrets are never shown | sensitive values are masked in tool results and reports | test |
| Everything is audited | `state/reviews.db` and the Remediation Agent's own audit | live: audit chains read back |

## 13. Problems found and fixed through validation

Validation against the live system found real problems that tests had not. Each was fixed and given a regression
test.

| Found during | Problem | Fix |
|---|---|---|
| Iteration 4 | The recorder's pod watch delivered events about 60 s late, so logs of ended runs were lost | detect lifecycle changes by comparing snapshots every 5 s |
| Iteration 6 | Refused approvals did not keep the typed value; the "symptoms cleared" state was lost on refresh; the plan's age was frozen | all fixed and tested |
| Iteration 7 | Recovery attributed to the wrong cause in the planned OOM demo | the demo was not run as planned (section 8) |
| `.env` recovery | `kubernetes` 37 broke a client call; Slack permalinks were rejected | version pinned; request fixed |
| Live run, 2026-11-10 | Short blips opened incidents; agents misread old events as causes | section 11 |
| Live run, 2026-11-11 | The sustain rule counted polls, which take 8–10 s, not 5 s | measured in seconds |
| Live run, 2026-11-11 | A cluster restart was reported as an incident | detection restarts after 90 s |

## 14. Known limitations

- **One provider.** Kubernetes is the only resource provider. The capability layer is designed for a second one, but
  provider independence is not claimed until a second provider exists (planned, Iteration 8).
- **Local, single cluster.** No multi-tenant control plane or customer connector yet (planned).
- **Small numbers.** Each live scenario was run a handful of times. These are demonstrations, not statistics.
- **Verification judges state, not cause.** An unrelated stall during the window gives NOT_RESOLVED; a recovery not
  caused by the change could give RESOLVED.
- **LLM agents on transient incidents.** They are weak on short blips with noisy history; the timing facts reduce
  this but do not remove it.
- **External model service.** NVIDIA's free endpoints are sometimes slow; the Investigator took up to 285 s when
  leading, and timed out twice before the fallback was added.
- **After a cluster restart,** the first status sample once took about 2 minutes while the cluster booted.
- **Slack thread content** is confirmed by the reviewer, not read back by the bot (it has no `channels:history`).

## 15. Headline results

| Result | Measure | Section |
|---|---|---|
| Offline diagnosis accuracy | **20 / 20** correct (10 scenarios, 4 of them deliberate traps, × 2 strategies) | 4 |
| Planner efficiency | the same answers with **~20% fewer** capability calls than collecting everything | 4 |
| Value of evidence history | a lost OOM went from **Undetermined, 0% → Memory exhaustion, 85%**; a wrong diagnosis was corrected | 5 |
| Automated tests | **273**, all passing, in about 17 s, offline (from 7 at Iteration 2) | 3 |
| Design rules enforced by tests | **11** architecture tests (for example: only the executor can change the cluster) | 3, 12 |
| Detection latency | every incident opened **7–30 s** after the injection | 9.1 |
| Time to a decision-ready plan | **68–224 s** after detection, with two LLM agents | 9.2 |
| Investigator evidence quality | **55 / 55** findings supported by collected evidence | 9.2 |
| Automatic remediation | **6 / 6** gate-eligible runs resolved on the first attempt (Database down ×3, Memory limit too low ×3); nothing applied without a passed gate | 9.2, 10, 11 |
| Engineer-approved remediation | **4 / 4** approved fixes the Reviewer agreed with were RESOLVED; the one executed against its challenge came back NOT_RESOLVED, as it predicted | 10 |
| Reviewer Agent | **23** live reviews: 20 agreements, **3 evidence-backed challenges**, 1 of them stopping a wrong automatic fix | 9.2, 10 |
| False alarms | **0** in 10 min idle and **0** from 7 induced stalls (5xx up to 45%), down from 2 in about 3 h before the fix | 11 |
| Resilience | recovered by itself from a killed metrics connection and from a full cluster restart, with no false incident | 11 |
| Safety | every change behind approval (or a passed gate), policy, live recheck, dry run, **exactly one** typed change and verification; a failed fix is never retried | 7, 12 |

## 16. How to reproduce

```bash
pip install -r requirements.txt fastapi uvicorn
python -m pytest -q
minikube start -p incident-demo
python -m demo
```

- Offline tests and the scenario benchmark need no cluster or network.
- The measurement scripts are in `scripts/measure/`:
  - `offline_bench.py`: section 4 (`python scripts/measure/offline_bench.py`);
  - `run_incident.py <incident>`: one incident end to end against the running console (sections 9 and 10);
  - `false_alarms.py <out.json> <idle seconds>`: the idle and stall measurements of section 11.
- The live runs need Docker, the minikube cluster set up with `scripts/setup.ps1`, a `.env` with the Slack and
  NVIDIA settings (`.env.example` lists them), and the console at http://127.0.0.1:8800.
- Detailed live records: `docs/validation/`.
