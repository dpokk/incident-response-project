# Iteration 7 live demonstrations: approved execution + verification

Date: 2026-10-01. Cluster: minikube `incident-demo`, namespace `shop`.

The code ran from the `feature/iter-07-approved-remediation-execution` worktree: `python -m investigator watch` with
the Slack listener (Socket Mode), Execute enabled. There was one approver/executor (the default: executors =
approvers). The policy was `config/execution_policy.json`: 15 min max plan age; settle = 3 consecutive ready samples,
max 120 s; 120 s observation window.

For every demo, a Kubernetes snapshot (generation, replicas, memory limit, restart annotation, ConfigMap host,
resourceVersions) was taken before the injection and after verification. The audit chain was read back from
`state/reviews.db`, and in every case the recomputed plan digest matched the approved one.

## Summary

| Demo | Incident | Approved change | Execution | Outcome |
|---|---|---|---|---|
| B: PostgreSQL unavailable | INC-20261001-212836 | scale `postgres` 0 → 1 (engineer-supplied) | c50311c6c39e68a85c08 | **RESOLVED** |
| C: wrong DB host, first run | INC-20261001-213412 | restore `DATABASE_URL` host `postgres-wrong` → recorded value `postgres` | bcebaf1f5754dc358122 | **NOT_RESOLVED** (see below) |
| C: wrong DB host, re-run | INC-20261001-214012 | same | 4cc0482f80819fa08bc8 | **RESOLVED** |
| D: failed remediation | INC-20261001-214507 | backend memory 192Mi → 256Mi under sustained overload (210 rps) | 91361f5ec4343df18da4 | **NOT_RESOLVED** (as predicted) |
| D: rollback | INC-20261001-214507#rollback-91361f5e | backend memory 256Mi → 192Mi (recorded previous value) | 9c3c9e185c750407f3f7 | **RESOLVED** (backend ready) |
| A: OOM → higher limit | — | — | — | **not run**; see "Demo A" |

## Demo B: PostgreSQL scaled to 0

- **Before:** `Deployment/postgres` generation 11, 1 replica. Injected by scaling to 0.
- **Diagnosis and plan:** dependency unavailable in backend (57%). Plan `d49151cb9d0e`: `scale_workload postgres`,
  with `target_replicas` left for the engineer.
- **Human steps:** approved with `1`, then Execute.
- **Execution chain:**
  - **Policy:** allowed (≤ 3 replicas).
  - **Recheck:** "postgres still has 0 desired replica(s), as the plan expected".
  - **Dry run:** accepted.
  - **Applied:** `PATCH spec.replicas=1`.
- **Verification:** settled after 26 s, then 120 s observation. All 5 criteria passed on every sample:
  - 1 ready endpoint;
  - postgres 1/1 ready;
  - 0 dependency error lines;
  - synthetic requests HTTP 200;
  - error ratio 0.0%.
- **After:** only `Deployment/postgres` changed (generation 13: one generation for the injection, one for the
  execution). Backend, ConfigMap, Secret and Services were unchanged.

## Demo C: wrong database host

**Plan.** The evidence recorder had captured the ConfigMap change, so the plan restored the **recorded previous
value**. The plan was complete; no value had to be typed. The executor replaced the host only, then restarted the
backend (one typed action, two API operations, both dry-run first).

**First run: NOT_RESOLVED, and a correct verdict.**
- **The change worked:** the host was restored and the new pods were ready 6 s after the change. There were 0
  dependency errors during observation.
- **Then both backend pods stalled at once:** at 21:36:26–34, about 50 s into observation, throughput fell to
  ~12 rps and in-flight requests rose to ~160. The frontend logged 124 upstream timeouts, one synthetic request
  returned 504, and the error ratio peaked at 15.1%.
- **Likely cause:** the stall coincided with a PostgreSQL checkpoint (21:36:15–21:37:07, 488 buffers). This was
  not proven, since no node metrics are available. The stall was unrelated to the restored configuration.
- **Verdict:** the plan's criterion "error ratio stays below 5%" was violated during the window, so the verdict was
  correct as specified. The verifier does not separate an unrelated stall from an incomplete fix, and does not turn
  doubt into success.
- **Failure handling:**
  - no further change was made;
  - a rollback plan (`postgres` → `postgres-wrong`) was posted for review;
  - the rollback was deliberately **not** approved, because executing it would have re-broken the dependency.

**Re-run: RESOLVED.**
- **Execution chain:**
  - **Recheck:** "still `postgres-wrong`, as the plan expected"; "`postgres` exists with 1 ready endpoint".
  - **Dry run:** both operations accepted.
  - **Applied:** both operations.
- **Verification:** settled after 21 s. All 4 criteria passed on every sample (0 dependency errors, 0.0% error
  ratio).
- **After:** the ConfigMap host was `postgres`. Backend generation +1 for the injection's restart and +1 for the
  executor's restart.

## Demo D: deliberately insufficient remediation

- **Injection:** sustained 210 rps, above the backend's ~190 rps capacity (see `iter-07-demo-a-calibration.md`).
- **Plan:** memory exhaustion (97%). Its own risk statement says "if that growth is unbounded (e.g. an unbounded
  request backlog), a higher limit only delays the next kill".
- **Human steps:** approved `256Mi`, then Execute.
- **Execution chain:**
  - **Recheck:** "still 192Mi, as the plan expected; 0/2 ready, failing now; 10 kills at the memory limit in the
    last 15 min".
  - **Dry run:** accepted.
  - **Applied:** 192Mi → 256Mi.
- **Verification:** not settled within 120 s. All 4 criteria failed:
  - backend never all ready;
  - **8 new OOM kills in pods started after the change**;
  - synthetic requests HTTP 504;
  - error ratio 100%.
- **Outcome:** **NOT_RESOLVED**.
- **Afterwards:**
  - **No further change.** The log shows exactly one `applied` line for this incident.
  - **Rollback offered:** a typed rollback plan was posted for review.
- **Rollback:**
  1. The injected load was returned to 100 rps.
  2. The rollback plan was approved separately (plan `0a34a943ea16`, its own review key), then Execute.
  3. Execution chain: recheck "memory limit is still 256Mi, as the plan expected" (the relevance requirement is
     not applied to a rollback); dry run accepted; applied 256Mi → 192Mi.
  4. Verification: backend 2/2 ready on 25/25 samples → RESOLVED. No further rollback was offered.
- **Final state:** every value is back to the original baseline (backend 192Mi, postgres 1 replica, host
  `postgres`). Only generations, resourceVersions and the restart annotation differ.

## Demo A: not run

Two live attempts at 170 rps (21:24 and 21:26) followed the calibrated injection, a spike to trigger the OOM cascade.
Both times the backend recovered by itself before the evidence was collected: both pods were killed, backed off and
restarted together. The plans therefore said "no immediate action" (a preventive limit change), and they were not
executed, because executing would have "resolved" an already recovered system.

This matches calibration finding 3: the persistent cascade depends on staggered restarts, which cannot be produced on
demand. Demo A is deferred, by agreement, to be revisited.

## Observed limitations

- **Duplicate incidents.** While a failure persists, `watch` can open further incidents when new kinds of signals
  appear after a report, or when a sample happens to look healthy (19:46–19:48: three threads for one OOM). This is
  existing detection behaviour, not caused by execution.
- **Suppression not exercised live.** The scoped expected-effect suppression did not need to act in these demos: the
  executing incident stayed open, so no new incident came from the rollout. It is covered by tests only.
- **Verification judges observed state, not cause.** An unrelated transient degradation inside the window gives
  NOT_RESOLVED (Demo C first run), and a recovery not caused by the change could give RESOLVED (calibration E7).
  This is reported, never hidden.
- **Thread content not read back.** The bot has no `channels:history`. The Slack thread content was confirmed by the
  reviewer, not read back.
