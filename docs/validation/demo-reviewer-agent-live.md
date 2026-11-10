# Reviewer Agent — live run of every demo incident (2026-11-10)

Branch `feat/reviewer-agent`, local minikube `incident-demo`, Slack connected, automatic remediation ON.
Each incident was triggered from the console, run to its stop point, then reset to healthy. Engineer decisions
were made in Slack by the configured approver.

Models: the Investigator Agent used `nvidia/nemotron-3-ultra-550b-a55b`, and the Reviewer Agent used
`nvidia/nemotron-3.5-super-vl-120b-a12b` (the fallback was not needed).

| Incident | Rule engine | Route | Investigator Agent (fix · evidence · calls · s) | Reviewer Agent (verdict · calls · s) | Gate | Result |
|---|---|---|---|---|---|---|
| Database down | high | verify | scale postgres → 1 · 6/6 · 4 · 39.5 | **agree** · 3 · 25.9 | passed | **resolved automatically** |
| Wrong database host | 97% | verify | restore `DATABASE_URL` · 6/6 · 5 · 64.6 | **agree** · 5 · 27.1 | refused (config restore needs a human) | Approve + Execute → RESOLVED |
| Memory exhaustion (spike) | 70% | lead | memory → 512Mi · 5/5 · 14 · 284.6 | **agree** · 3 · 49.4 | refused (agent led, < 85%) | Approve + Execute → RESOLVED |
| Stale database credentials | 70% | lead | restore `PGPASSWORD` secret · 6/6 · 14 · 140.2 | **agree** · 4 · 73.5 | refused (no typed action) | handed to engineer (manual steps) |
| Backend scaled to zero | undetermined | lead | scale backend → 2 · 6/6 · 14 · 195.8 | **agree** · 4 · 59.7 | refused (agent led) | Approve + Execute → RESOLVED |
| Memory limit set too low | 85% | verify | memory → 512Mi · 5/5 · 5 · 75.6 | **agree** · 4 · 56.0 | passed | **resolved automatically** (attempt 1, 512Mi) |
| Application crash | 90% | verify | investigate only · 5/5 · 5 · 57.3 | **agree** · 4 · 12.8 | refused (no typed action) | handed to engineer (manual steps) |
| Broken release | 90% | verify | roll back to `app:0.2` · 4/4 · 4 · 49.7 | **agree** · 4 · 36.3 | refused (rollback needs a human) | Approve + Execute → RESOLVED |
| **Sustained overload** | 85% | verify | memory → 512Mi · 5/5 · 4 · 58.6 | **challenge** · 4 · 73.8 | **refused only by the Reviewer** | Approve + Execute → **NOT_RESOLVED** |

No Slack message failed in any run.

## What the run shows

- **The Reviewer blocked a wrong automatic fix.** On Sustained overload, every other gate check passed: rules at 85%,
  the Investigator agreeing, all findings supported. Without the Reviewer, the Remediation Agent would have raised
  the memory limit by itself. The Reviewer challenged with three objections, all citing collected evidence:
  - the peak working set was ~40MiB against a 192Mi limit (E3);
  - the logs say "request backlog growing; workers cannot keep up" (E6);
  - nothing justifies 512Mi.

  The engineer executed the fix anyway, and verification returned NOT_RESOLVED, which confirms the challenge.
- **Agreeing changed nothing.** In the 8 incidents where it agreed, the flow was the same as without the Reviewer:
  two automatic fixes, four approvals in Slack, and two handovers.
- **Cost:** 13–74 s per incident (3–5 tool calls), with the 120B model.

## Not observed

- **reject:** no Investigator conclusion was wrong enough to reject. That path is covered by
  `tests/test_demo_reviewer.py`. An offline check against the real model rejected a planted wrong diagnosis (frontend
  OOM while PostgreSQL was at 0 replicas), with two supported objections.
- **The fallback to the 550B:** not needed.

## Notes

- On a cold cluster start, the backend and frontend crash-looped until PostgreSQL was ready. That opened a real
  incident (INC-20261110-212857). On it, the Investigator's 550B request timed out on NVIDIA's side after ~290 s.
  As designed, the rule engine's findings stood and the Reviewer reported that there was no report to review.
- "Investigator does not agree with the rule engine" on Wrong database host is a naming mismatch, not a
  disagreement: the rules name the component `DATABASE_URL (configmap/backend-config)`, the agent names `backend`.
  This existed before the Reviewer.
